"""Durable local outbox for Core autonomy supervisor audit events."""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import stat
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, time
from typing import Any, Literal
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX_PATH = Path.home() / ".local" / "state" / "amiki" / "supervisor-events.sqlite3"
OUTBOX_PATH_ENV = "AMIKI_CLOUD_SUPERVISOR_OUTBOX_PATH"
MAX_PENDING_EVENTS = 10_000
DELIVERED_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEAD_LETTER_RETENTION_SECONDS = 90 * 24 * 60 * 60
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_INITIAL_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 300.0
DEFAULT_LEASE_SECONDS = 30.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15.0
DEFAULT_BATCH_SIZE = 10
MAX_DEAD_LETTER_REPLAY_BATCH = 50
MAX_MANUAL_REPLAYS = 3

_SUPERVISOR_COMPONENTS = frozenset({"autonomy_worker", "supervisor_outbox_delivery"})
_EVENT_REASONS = {
    "restart_scheduled": frozenset(
        {"worker_exception", "worker_returned", "worker_start_failed", "owner_requested_recovery"}
    ),
    "restart_started": frozenset({"worker_started"}),
    "restart_exhausted": frozenset({"restart_budget_exhausted"}),
    "restart_cancelled": frozenset({"application_shutdown"}),
}
_EVENT_FIELDS = (
    "event_id",
    "supervisor_id",
    "component",
    "event_type",
    "attempt",
    "delay_seconds",
    "reason_code",
)


class CloudSupervisorEventOutbox:
    """Persist supervisor events before attempting bounded Cloud delivery.

    Delivery is at-least-once. The event UUID is stable across retries and Cloud
    deduplicates it, so an ambiguous network response cannot create duplicate
    audit records.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time,
    ) -> None:
        if path is None:
            configured_path = os.getenv(OUTBOX_PATH_ENV, "").strip()
            path = Path(configured_path).expanduser() if configured_path else DEFAULT_OUTBOX_PATH
        self.path = Path(path).expanduser()
        if not self.path.is_absolute():
            raise ValueError("supervisor_outbox_path_must_be_absolute")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ValueError("supervisor_outbox_attempt_limit_out_of_range")
        if not callable(clock):
            raise ValueError("supervisor_outbox_clock_invalid")
        self._max_attempts = max_attempts
        self._initial_backoff_seconds = self._bounded_number(
            "supervisor_outbox_initial_backoff_out_of_range",
            initial_backoff_seconds,
            0.1,
            60.0,
        )
        self._max_backoff_seconds = self._bounded_number(
            "supervisor_outbox_max_backoff_out_of_range",
            max_backoff_seconds,
            0.1,
            3600.0,
        )
        self._lease_seconds = self._bounded_number(
            "supervisor_outbox_lease_out_of_range",
            lease_seconds,
            5.0,
            300.0,
        )
        if self._max_backoff_seconds < self._initial_backoff_seconds:
            raise ValueError("supervisor_outbox_backoff_range_invalid")
        self._clock = clock
        self._owner_id = str(uuid4())
        self._initialize()

    @property
    def instance_id(self) -> str:
        """Return the stable UUID for this running outbox instance."""
        return self._owner_id

    @staticmethod
    def _bounded_number(
        error_code: str,
        value: float,
        minimum: float,
        maximum: float,
    ) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not minimum <= float(value) <= maximum
        ):
            raise ValueError(error_code)
        return float(value)

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise RuntimeError("supervisor_outbox_clock_invalid")
        return float(value)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 2000")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise ValueError("supervisor_outbox_directory_invalid")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = parent.stat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ValueError("supervisor_outbox_directory_permissions_invalid")

        if self.path.is_symlink():
            raise ValueError("supervisor_outbox_file_must_not_be_symlink")
        if self.path.exists():
            file_info = self.path.stat()
            if not stat.S_ISREG(file_info.st_mode):
                raise ValueError("supervisor_outbox_file_invalid")
            if hasattr(os, "getuid") and file_info.st_uid != os.getuid():
                raise ValueError("supervisor_outbox_file_owner_invalid")
            if stat.S_IMODE(file_info.st_mode) & 0o077:
                os.chmod(self.path, 0o600)
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)

        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_event_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'delivered', 'dead_letter')),
                    last_error_code TEXT,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    delivered_at REAL,
                    dead_lettered_at REAL,
                    manual_replays INTEGER NOT NULL DEFAULT 0 CHECK (manual_replays >= 0),
                    last_manual_replay_at REAL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(supervisor_event_outbox)"
                )
            }
            if "manual_replays" not in columns:
                connection.execute(
                    """
                    ALTER TABLE supervisor_event_outbox
                    ADD COLUMN manual_replays INTEGER NOT NULL DEFAULT 0
                        CHECK (manual_replays >= 0)
                    """
                )
            if "last_manual_replay_at" not in columns:
                connection.execute(
                    """
                    ALTER TABLE supervisor_event_outbox
                    ADD COLUMN last_manual_replay_at REAL
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_supervisor_event_outbox_due
                ON supervisor_event_outbox (state, next_attempt_at, lease_expires_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_supervisor_event_outbox_dead_letter
                ON supervisor_event_outbox (state, dead_lettered_at, event_id)
                """
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _normalize_event(event: Mapping[str, object]) -> dict[str, Any]:
        if not isinstance(event, Mapping):
            raise ValueError("supervisor_outbox_event_invalid")

        try:
            event_id = str(UUID(event.get("event_id")))
            supervisor_id = str(UUID(event.get("supervisor_id")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("supervisor_outbox_event_id_invalid") from exc

        component = event.get("component", "autonomy_worker")
        event_type = event.get("event_type")
        reason_code = event.get("reason_code")
        if not isinstance(component, str) or component not in _SUPERVISOR_COMPONENTS:
            raise ValueError("supervisor_outbox_component_invalid")
        if not isinstance(event_type, str) or event_type not in _EVENT_REASONS:
            raise ValueError("supervisor_outbox_event_type_invalid")
        if not isinstance(reason_code, str) or reason_code not in _EVENT_REASONS[event_type]:
            raise ValueError("supervisor_outbox_reason_code_invalid")

        attempt = event.get("attempt")
        if type(attempt) is not int or not 1 <= attempt <= 11:
            raise ValueError("supervisor_outbox_attempt_out_of_range")
        delay_seconds = event.get("delay_seconds")
        if (
            isinstance(delay_seconds, bool)
            or not isinstance(delay_seconds, (int, float))
            or not math.isfinite(float(delay_seconds))
            or not 0 <= float(delay_seconds) <= 300
        ):
            raise ValueError("supervisor_outbox_delay_out_of_range")

        return {
            "event_id": event_id,
            "supervisor_id": supervisor_id,
            "component": component,
            "event_type": event_type,
            "attempt": attempt,
            "delay_seconds": round(float(delay_seconds), 3),
            "reason_code": reason_code,
        }

    @staticmethod
    def _prune_expired_rows(connection: sqlite3.Connection, now: float) -> None:
        """Apply terminal-event retention without ever deleting pending work."""
        connection.execute(
            """
            DELETE FROM supervisor_event_outbox
            WHERE state = 'delivered' AND delivered_at < ?
            """,
            (now - DELIVERED_RETENTION_SECONDS,),
        )
        connection.execute(
            """
            DELETE FROM supervisor_event_outbox
            WHERE state = 'dead_letter' AND dead_lettered_at < ?
            """,
            (now - DEAD_LETTER_RETENTION_SECONDS,),
        )

    def enqueue(self, event: Mapping[str, object]) -> bool:
        """Durably enqueue one allowlisted event; identical replays are idempotent."""
        payload = self._normalize_event(event)
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = self._now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT payload_json FROM supervisor_event_outbox WHERE event_id = ?",
                    (payload["event_id"],),
                ).fetchone()
                if existing is not None:
                    if str(existing["payload_json"]) != payload_json:
                        raise ValueError("supervisor_outbox_event_id_conflict")
                    connection.commit()
                    return True

                self._prune_expired_rows(connection, now)
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM supervisor_event_outbox WHERE state = 'pending'"
                ).fetchone()
                if int(pending["count"]) >= MAX_PENDING_EVENTS:
                    raise RuntimeError("supervisor_outbox_capacity_reached")

                connection.execute(
                    """
                    INSERT INTO supervisor_event_outbox (
                        event_id, payload_json, created_at, next_attempt_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (payload["event_id"], payload_json, now, now),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def _claim_due_event(self) -> dict[str, Any] | None:
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune_expired_rows(connection, now)
            connection.execute(
                """
                UPDATE supervisor_event_outbox
                SET state = 'dead_letter',
                    last_error_code = 'delivery_interrupted',
                    dead_lettered_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL
                WHERE state = 'pending'
                  AND attempts >= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, self._max_attempts, now),
            )
            row = connection.execute(
                """
                SELECT event_id, payload_json, attempts
                FROM supervisor_event_outbox
                WHERE state = 'pending'
                  AND attempts < ?
                  AND next_attempt_at <= ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at, event_id
                LIMIT 1
                """,
                (self._max_attempts, now, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE supervisor_event_outbox
                SET attempts = ?, lease_owner = ?, lease_expires_at = ?
                WHERE event_id = ? AND state = 'pending'
                """,
                (
                    attempts,
                    self._owner_id,
                    now + self._lease_seconds,
                    str(row["event_id"]),
                ),
            )
            connection.commit()

        return {
            "event_id": str(row["event_id"]),
            "payload_json": str(row["payload_json"]),
            "attempts": attempts,
        }

    def _finish_delivery(self, event_id: str, attempts: int, accepted: bool) -> None:
        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if accepted:
                connection.execute(
                    """
                    UPDATE supervisor_event_outbox
                    SET state = 'delivered',
                        delivered_at = ?,
                        last_error_code = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE event_id = ? AND state = 'pending' AND lease_owner = ?
                    """,
                    (now, event_id, self._owner_id),
                )
            elif attempts >= self._max_attempts:
                connection.execute(
                    """
                    UPDATE supervisor_event_outbox
                    SET state = 'dead_letter',
                        last_error_code = 'delivery_unconfirmed',
                        dead_lettered_at = ?,
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE event_id = ? AND state = 'pending' AND lease_owner = ?
                    """,
                    (now, event_id, self._owner_id),
                )
            else:
                delay = min(
                    self._max_backoff_seconds,
                    self._initial_backoff_seconds * (2 ** (attempts - 1)),
                )
                connection.execute(
                    """
                    UPDATE supervisor_event_outbox
                    SET next_attempt_at = ?,
                        last_error_code = 'delivery_unconfirmed',
                        lease_owner = NULL,
                        lease_expires_at = NULL
                    WHERE event_id = ? AND state = 'pending' AND lease_owner = ?
                    """,
                    (now + delay, event_id, self._owner_id),
                )
            connection.commit()

    def deliver_one(self, deliver: Callable[[Mapping[str, object]], bool]) -> bool:
        """Attempt one due event; return false when no event is currently claimable."""
        if not callable(deliver):
            raise ValueError("supervisor_outbox_delivery_callback_invalid")
        claimed = self._claim_due_event()
        if claimed is None:
            return False

        try:
            payload = json.loads(claimed["payload_json"])
            accepted = deliver(payload) is True
        except Exception:
            accepted = False
        self._finish_delivery(claimed["event_id"], claimed["attempts"], accepted)
        return True

    def retry_dead_letters(self, event_ids: list[str]) -> dict[str, int]:
        """Requeue selected dead letters with stable IDs and bounded manual replay."""
        if (
            not isinstance(event_ids, list)
            or not 1 <= len(event_ids) <= MAX_DEAD_LETTER_REPLAY_BATCH
        ):
            raise ValueError("supervisor_outbox_retry_batch_out_of_range")

        normalized_ids: list[str] = []
        seen: set[str] = set()
        for event_id in event_ids:
            try:
                normalized = str(UUID(event_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValueError("supervisor_outbox_retry_event_id_invalid") from exc
            if normalized in seen:
                raise ValueError("supervisor_outbox_retry_event_id_duplicate")
            seen.add(normalized)
            normalized_ids.append(normalized)

        now = self._now()
        requeued = 0
        not_retryable = 0
        not_found = 0
        replay_limit_reached = 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._prune_expired_rows(connection, now)
                pending_row = connection.execute(
                    "SELECT COUNT(*) AS count FROM supervisor_event_outbox "
                    "WHERE state = 'pending'"
                ).fetchone()
                pending_count = int(pending_row["count"])
                for event_id in normalized_ids:
                    row = connection.execute(
                        """
                        SELECT state, manual_replays
                        FROM supervisor_event_outbox
                        WHERE event_id = ?
                        """,
                        (event_id,),
                    ).fetchone()
                    if row is None:
                        not_found += 1
                        continue
                    if str(row["state"]) != "dead_letter":
                        not_retryable += 1
                        continue
                    if int(row["manual_replays"]) >= MAX_MANUAL_REPLAYS:
                        replay_limit_reached += 1
                        continue
                    if pending_count >= MAX_PENDING_EVENTS:
                        raise RuntimeError("supervisor_outbox_capacity_reached")

                    cursor = connection.execute(
                        """
                        UPDATE supervisor_event_outbox
                        SET state = 'pending',
                            attempts = 0,
                            next_attempt_at = ?,
                            last_error_code = NULL,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            delivered_at = NULL,
                            dead_lettered_at = NULL,
                            manual_replays = manual_replays + 1,
                            last_manual_replay_at = ?
                        WHERE event_id = ? AND state = 'dead_letter'
                        """,
                        (now, now, event_id),
                    )
                    if cursor.rowcount == 1:
                        requeued += 1
                        pending_count += 1
                    else:
                        not_retryable += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return {
            "requeued": requeued,
            "not_retryable": not_retryable,
            "not_found": not_found,
            "replay_limit_reached": replay_limit_reached,
        }

    def list_dead_letter_ids(self, limit: int = 50) -> list[str]:
        """Return bounded dead-letter IDs only; never expose queued payloads."""
        if type(limit) is not int or not 1 <= limit <= MAX_DEAD_LETTER_REPLAY_BATCH:
            raise ValueError("supervisor_outbox_dead_letter_limit_out_of_range")

        now = self._now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._prune_expired_rows(connection, now)
                rows = connection.execute(
                    """
                    SELECT event_id
                    FROM supervisor_event_outbox
                    WHERE state = 'dead_letter'
                    ORDER BY dead_lettered_at ASC, event_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return [str(row["event_id"]) for row in rows]

    def snapshot(self) -> dict[str, Any]:
        """Return bounded counts only; never expose queued event payloads or paths."""
        now = self._now()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT state, COUNT(*) AS count
                FROM supervisor_event_outbox
                GROUP BY state
                """
            ).fetchall()
            oldest = connection.execute(
                """
                SELECT MIN(created_at) AS created_at
                FROM supervisor_event_outbox
                WHERE state = 'pending'
                """
            ).fetchone()

        counts = {str(row["state"]): int(row["count"]) for row in rows}
        oldest_created_at = oldest["created_at"]
        oldest_age = (
            None
            if oldest_created_at is None
            else round(max(0.0, now - float(oldest_created_at)), 3)
        )
        return {
            "available": True,
            "pending": counts.get("pending", 0),
            "delivered": counts.get("delivered", 0),
            "dead_lettered": counts.get("dead_letter", 0),
            "oldest_pending_age_seconds": oldest_age,
            "max_attempts": self._max_attempts,
        }

    def run(
        self,
        stop_event: threading.Event,
        deliver: Callable[[Mapping[str, object]], bool],
        *,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        heartbeat: Callable[[str, Literal["running", "stopped"]], bool] | None = None,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        """Drain due events and report delivery-thread liveness until shutdown."""
        interval = self._bounded_number(
            "supervisor_outbox_poll_interval_out_of_range",
            poll_interval_seconds,
            0.1,
            30.0,
        )
        heartbeat_period = self._bounded_number(
            "supervisor_outbox_heartbeat_interval_out_of_range",
            heartbeat_interval_seconds,
            5.0,
            60.0,
        )
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("supervisor_outbox_batch_size_out_of_range")
        if not callable(deliver):
            raise ValueError("supervisor_outbox_delivery_callback_invalid")
        if heartbeat is not None and not callable(heartbeat):
            raise ValueError("supervisor_outbox_heartbeat_callback_invalid")

        def report_heartbeat(state: Literal["running", "stopped"]) -> bool:
            if heartbeat is None:
                return False
            try:
                accepted = heartbeat(self.instance_id, state)
            except Exception:
                logger.warning("amiki_cloud_supervisor_event_outbox_heartbeat_failed")
                return False
            if accepted is not True:
                logger.warning("amiki_cloud_supervisor_event_outbox_heartbeat_unconfirmed")
                return False
            return True

        next_heartbeat_at = 0.0
        heartbeat_confirmed = False
        completed_normally = False
        try:
            while not stop_event.is_set():
                if heartbeat is not None and monotonic() >= next_heartbeat_at:
                    heartbeat_confirmed = report_heartbeat("running") or heartbeat_confirmed
                    next_heartbeat_at = monotonic() + heartbeat_period

                processed = 0
                try:
                    while processed < batch_size and not stop_event.is_set():
                        if not self.deliver_one(deliver):
                            break
                        processed += 1
                except Exception:
                    logger.warning("amiki_cloud_supervisor_event_outbox_cycle_failed")
                if processed == 0:
                    wait_seconds = interval
                    if heartbeat is not None:
                        wait_seconds = min(
                            wait_seconds,
                            max(0.0, next_heartbeat_at - monotonic()),
                        )
                    stop_event.wait(wait_seconds)
            completed_normally = True
        finally:
            # A graceful stop is reported only after normal loop exit. An unexpected
            # thread failure must leave the last running heartbeat stale for Cloud.
            if completed_normally and heartbeat_confirmed:
                report_heartbeat("stopped")


__all__ = ["CloudSupervisorEventOutbox"]
