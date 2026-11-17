"""Bounded supervisor for the optional Amiki Cloud autonomy dispatcher."""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESTARTS = 3
DEFAULT_RESTART_WINDOW_SECONDS = 300.0
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_SUPERVISOR_POLL_SECONDS = 0.1
DEFAULT_RESTART_HISTORY_LIMIT = 10
_ALLOWED_COMPONENTS = frozenset({"autonomy_worker", "supervisor_outbox_delivery"})


class CloudAutonomyWorkerSupervisor:
    """Restart an unexpectedly exited worker within a bounded retry budget.

    The supervisor owns a separate stop event for each worker attempt. The
    application stop event is authoritative: once set, no new worker attempt is
    started, and the current attempt receives a cooperative stop request.
    """

    def __init__(
        self,
        worker: Callable[[threading.Event], None],
        *,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        restart_window_seconds: float = DEFAULT_RESTART_WINDOW_SECONDS,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        poll_interval_seconds: float = DEFAULT_SUPERVISOR_POLL_SECONDS,
        history_limit: int = DEFAULT_RESTART_HISTORY_LIMIT,
        clock: Callable[[], float] = monotonic,
        event_reporter: Callable[[dict], bool] | None = None,
        component: str = "autonomy_worker",
    ) -> None:
        if not callable(worker):
            raise ValueError("worker_runner_required")
        if not callable(clock):
            raise ValueError("worker_supervisor_clock_required")
        if event_reporter is not None and not callable(event_reporter):
            raise ValueError("worker_supervisor_event_reporter_invalid")
        if not isinstance(component, str) or component not in _ALLOWED_COMPONENTS:
            raise ValueError("worker_supervisor_component_invalid")
        if type(max_restarts) is not int or not 0 <= max_restarts <= 10:
            raise ValueError("worker_restart_limit_out_of_range")
        if type(history_limit) is not int or not 1 <= history_limit <= 50:
            raise ValueError("worker_restart_history_limit_out_of_range")

        numeric_bounds = (
            ("restart_window_seconds", restart_window_seconds, 1.0, 86400.0),
            ("initial_backoff_seconds", initial_backoff_seconds, 0.01, 60.0),
            ("max_backoff_seconds", max_backoff_seconds, 0.01, 300.0),
            ("poll_interval_seconds", poll_interval_seconds, 0.01, 1.0),
        )
        for name, value, minimum, maximum in numeric_bounds:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not minimum <= float(value) <= maximum
            ):
                raise ValueError(f"{name}_out_of_range")
        if float(max_backoff_seconds) < float(initial_backoff_seconds):
            raise ValueError("max_backoff_must_cover_initial_backoff")

        self._worker = worker
        self._max_restarts = max_restarts
        self._restart_window_seconds = float(restart_window_seconds)
        self._initial_backoff_seconds = float(initial_backoff_seconds)
        self._max_backoff_seconds = float(max_backoff_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._clock = clock
        self._event_reporter = event_reporter
        self._component = component
        self._supervisor_id = str(uuid4())
        self._history: deque[dict] = deque(maxlen=history_limit)
        self._restart_times: deque[float] = deque()
        self._lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._supervisor_thread: threading.Thread | None = None
        self._state = "stopped"
        self._supervisor_running = False
        self._restart_count = 0
        self._last_exit_reason: str | None = None

    def snapshot(self) -> dict:
        """Return bounded, secret-free supervisor state for operational APIs."""
        with self._lock:
            worker_running = bool(
                self._worker_thread is not None and self._worker_thread.is_alive()
            )
            return {
                "supervisor_id": self._supervisor_id,
                "component": self._component,
                "state": self._state,
                "supervisor_running": self._supervisor_running,
                "worker_running": worker_running,
                "restart_count": self._restart_count,
                "restart_attempts_in_window": len(self._restart_times),
                "restart_limit": self._max_restarts,
                "restart_window_seconds": self._restart_window_seconds,
                "last_exit_reason": self._last_exit_reason,
                "history": [dict(item) for item in self._history],
            }

    @property
    def supervisor_thread(self) -> threading.Thread | None:
        """Return the managed supervisor thread, if it has been started."""
        with self._lock:
            return self._supervisor_thread

    def start(
        self,
        stop_event: threading.Event,
        *,
        thread_name: str = "amiki-cloud-autonomy-supervisor",
        allow_exhausted_restart: bool = False,
    ) -> bool:
        """Start the supervisor, optionally after an explicit exhausted-state recovery.

        An exhausted restart clears only the rolling-window budget. Cumulative
        restart history remains available for diagnostics. The operator recovery
        is recorded through the same content-free event outbox as other lifecycle
        transitions.
        """
        if not callable(getattr(stop_event, "is_set", None)):
            raise ValueError("worker_supervisor_stop_event_invalid")
        if not isinstance(thread_name, str) or not thread_name.strip():
            raise ValueError("worker_supervisor_thread_name_invalid")
        if type(allow_exhausted_restart) is not bool:
            raise ValueError("worker_supervisor_restart_flag_invalid")

        start_gate = threading.Event()
        recovery_attempt: int | None = None
        previous_restart_times: deque[float] | None = None
        previous_exit_reason: str | None = None

        def run_after_start_event() -> None:
            start_gate.wait()
            self.run(stop_event)

        with self._lock:
            if stop_event.is_set():
                return False
            if self._supervisor_thread is not None and self._supervisor_thread.is_alive():
                return False
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return False

            was_exhausted = self._state == "exhausted"
            if was_exhausted and not allow_exhausted_restart:
                return False
            if self._state not in {"stopped", "exhausted"}:
                return False

            if was_exhausted:
                previous_restart_times = deque(self._restart_times)
                previous_exit_reason = self._last_exit_reason
                self._restart_times.clear()
                self._restart_count += 1
                self._last_exit_reason = "owner_requested_recovery"
                recovery_attempt = self._restart_count

            self._state = "starting"
            self._supervisor_running = True
            thread = threading.Thread(
                target=run_after_start_event,
                name=thread_name,
                daemon=True,
            )
            self._supervisor_thread = thread

        try:
            thread.start()
        except RuntimeError:
            with self._lock:
                if self._supervisor_thread is thread:
                    self._supervisor_thread = None
                    self._state = "exhausted" if was_exhausted else "stopped"
                    self._supervisor_running = False
                    if was_exhausted:
                        self._restart_times = previous_restart_times or deque()
                        self._restart_count -= 1
                        self._last_exit_reason = previous_exit_reason
            return False

        if recovery_attempt is not None:
            with self._lock:
                self._append_history_locked(
                    attempt=recovery_attempt,
                    delay_seconds=0.0,
                    reason="owner_requested_recovery",
                )
            self._emit_event(
                event_type="restart_scheduled",
                attempt=recovery_attempt,
                delay_seconds=0.0,
                reason_code="owner_requested_recovery",
            )
        start_gate.set()
        return True

    def _append_history_locked(
        self,
        *,
        attempt: int,
        delay_seconds: float,
        reason: str,
    ) -> None:
        self._history.append(
            {
                "attempt": attempt,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "delay_seconds": round(delay_seconds, 3),
                "reason": reason,
            }
        )

    def _emit_event(
        self,
        *,
        event_type: str,
        attempt: int,
        delay_seconds: float,
        reason_code: str,
    ) -> None:
        """Report bounded lifecycle metadata without affecting worker recovery."""
        if self._event_reporter is None:
            return
        event = {
            "event_id": str(uuid4()),
            "supervisor_id": self._supervisor_id,
            "component": self._component,
            "event_type": event_type,
            "attempt": attempt,
            "delay_seconds": round(delay_seconds, 3),
            "reason_code": reason_code,
        }
        try:
            accepted = self._event_reporter(event)
        except Exception:
            accepted = False
        if accepted is not True:
            logger.warning("amiki_cloud_autonomy_supervisor_event_delivery_unconfirmed")

    def _run_worker_attempt(self, worker_stop: threading.Event) -> None:
        try:
            self._worker(worker_stop)
        except Exception:
            # Exception text may contain user content or provider details. Keep
            # the operational reason stable and deliberately non-sensitive.
            reason = "worker_exception"
            logger.error("amiki_cloud_autonomy_worker_crashed")
        else:
            reason = "worker_returned"
        with self._lock:
            self._last_exit_reason = reason

    def run(self, stop_event: threading.Event) -> None:
        """Supervise worker attempts until the application explicitly stops."""
        with self._lock:
            self._supervisor_running = not stop_event.is_set()
            self._state = "starting" if self._supervisor_running else "stopped"

        last_delay_seconds = 0.0
        while not stop_event.is_set():
            worker_stop = threading.Event()
            worker_thread = threading.Thread(
                target=self._run_worker_attempt,
                args=(worker_stop,),
                name="amiki-cloud-autonomy-worker-attempt",
                daemon=True,
            )
            with self._lock:
                self._worker_thread = worker_thread
                self._last_exit_reason = None
                self._state = "starting"

            try:
                worker_thread.start()
            except RuntimeError:
                with self._lock:
                    self._last_exit_reason = "worker_start_failed"
            else:
                with self._lock:
                    self._state = "running"
                    restart_attempt = self._restart_count
                if restart_attempt:
                    self._emit_event(
                        event_type="restart_started",
                        attempt=restart_attempt,
                        delay_seconds=last_delay_seconds,
                        reason_code="worker_started",
                    )

                while worker_thread.is_alive():
                    if stop_event.wait(self._poll_interval_seconds):
                        with self._lock:
                            self._state = "stopping"
                        worker_stop.set()
                        worker_thread.join(timeout=5.0)
                        with self._lock:
                            self._state = (
                                "stopped" if not worker_thread.is_alive() else "stop_timeout"
                            )
                            self._supervisor_running = False
                        return
                    worker_thread.join(timeout=0)

            worker_stop.set()
            if stop_event.is_set():
                break

            reason = self._last_exit_reason or "worker_returned"
            now = self._clock()
            budget_exhausted = False
            with self._lock:
                while (
                    self._restart_times
                    and now - self._restart_times[0] > self._restart_window_seconds
                ):
                    self._restart_times.popleft()

                if len(self._restart_times) >= self._max_restarts:
                    self._state = "exhausted"
                    self._supervisor_running = False
                    self._last_exit_reason = reason
                    exhausted_attempt = self._restart_count + 1
                    self._append_history_locked(
                        attempt=exhausted_attempt,
                        delay_seconds=0.0,
                        reason="restart_budget_exhausted",
                    )
                    budget_exhausted = True
                else:
                    self._restart_times.append(now)
                    attempt_in_window = len(self._restart_times)
                    self._restart_count += 1
                    restart_attempt = self._restart_count
                    delay = min(
                        self._max_backoff_seconds,
                        self._initial_backoff_seconds * (2 ** (attempt_in_window - 1)),
                    )
                    self._state = "backoff"
                    self._last_exit_reason = reason
                    self._append_history_locked(
                        attempt=restart_attempt,
                        delay_seconds=delay,
                        reason=reason,
                    )

            if budget_exhausted:
                self._emit_event(
                    event_type="restart_exhausted",
                    attempt=exhausted_attempt,
                    delay_seconds=0.0,
                    reason_code="restart_budget_exhausted",
                )
                logger.error("amiki_cloud_autonomy_worker_restart_budget_exhausted")
                return

            last_delay_seconds = delay
            self._emit_event(
                event_type="restart_scheduled",
                attempt=restart_attempt,
                delay_seconds=delay,
                reason_code=reason,
            )
            logger.warning(
                "amiki_cloud_autonomy_worker_restart_scheduled "
                "attempt=%s delay_seconds=%s reason=%s",
                restart_attempt,
                delay,
                reason,
            )
            if stop_event.wait(delay):
                with self._lock:
                    self._state = "stopped"
                    self._supervisor_running = False
                self._emit_event(
                    event_type="restart_cancelled",
                    attempt=restart_attempt,
                    delay_seconds=delay,
                    reason_code="application_shutdown",
                )
                return

        with self._lock:
            self._state = "stopped"
            self._supervisor_running = False


__all__ = ["CloudAutonomyWorkerSupervisor"]
