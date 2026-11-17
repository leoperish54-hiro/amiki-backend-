"""Request-scoped processing trace for Amiki."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from core.observability import build_failure_signature, get_observability
from core.runtime_awareness import (
    begin_request,
    update_request,
    complete_request,
    fail_request,
    record_provider_observation,
)


@dataclass
class StageTrace:
    name: str
    status: str = "started"
    detail: Optional[str] = None


@dataclass
class ProviderAttempt:
    provider: str
    attempt: int
    status: str
    detail: Optional[str] = None


@dataclass
class RequestTrace:
    """Observable trace for one Amiki request."""

    request_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    current_stage: Optional[str] = None
    stages: List[StageTrace] = field(default_factory=list)

    selected_capability: Optional[str] = None

    provider_attempts: List[ProviderAttempt] = field(
        default_factory=list
    )

    current_provider: Optional[str] = None
    current_model: Optional[str] = None
    final_provider: Optional[str] = None
    final_model: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    _last_observability_event_id: Optional[str] = field(default=None, init=False, repr=False)

    def start_stage(self, name: str) -> None:
        self.current_stage = name
        self.stages.append(
            StageTrace(name=name, status="started")
        )

        if len(self.stages) == 1:
            begin_request(self)
            event = get_observability().emit({
                "event_type": "API_REQUEST_STARTED",
                "severity": "INFO",
                "source": "backend",
                "component": "request_trace",
                "summary": "API request started.",
                "status": "started",
                "correlation_id": self.request_id,
                "metadata": {"stage": name},
            })
            if event is not None:
                self._last_observability_event_id = event.event_id
        else:
            update_request(self)

    def complete_stage(
        self,
        name: str,
        detail: Optional[str] = None,
    ) -> None:
        for stage in reversed(self.stages):
            if stage.name == name and stage.status == "started":
                stage.status = "completed"
                stage.detail = detail
                break

        self.current_stage = None
        update_request(self)

    def fail_stage(
        self,
        name: str,
        detail: Optional[str] = None,
    ) -> None:
        for stage in reversed(self.stages):
            if stage.name == name and stage.status == "started":
                stage.status = "failed"
                stage.detail = detail
                break

        self.current_stage = None
        update_request(self)

    def record_provider_attempt(
        self,
        provider: str,
        attempt: int,
        status: str,
        detail: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.provider_attempts.append(
            ProviderAttempt(
                provider=provider,
                attempt=attempt,
                status=status,
                detail=detail,
            )
        )
        record_provider_observation(
            provider,
            status,
            request_id=self.request_id,
            model=model,
            detail=detail,
        )
        self.current_provider = provider
        if model is not None:
            self.current_model = model

        if status == "success":
            self.final_provider = provider
            if model is not None:
                self.final_model = model

        safe_detail = detail if isinstance(detail, str) else "provider attempt"
        metadata = {
            "provider": provider,
            "attempt": attempt,
            "model": model,
            "detail": safe_detail,
        }
        event_type = "MODEL_SUCCESS" if status == "success" else "MODEL_FAILURE"
        severity = "INFO" if status == "success" else "ERROR"
        if status == "failed":
            import re
            match = re.search(r"HTTP\s+(\d{3})", safe_detail)
            if match:
                metadata["http_status"] = int(match.group(1))
            metadata["error_class"] = "provider_failure"
        event_payload = {
            "event_type": event_type,
            "severity": severity,
            "source": "backend",
            "component": "model_router",
            "summary": (
                "Model provider attempt succeeded."
                if status == "success"
                else "Model provider attempt failed."
            ),
            "status": status,
            "correlation_id": self.request_id,
            "parent_event_id": self._last_observability_event_id,
            "metadata": metadata,
        }
        event = (
            get_observability().emit_failure(
                event_payload,
                category="MODEL_PROVIDER",
                confirmed_evidence=[
                    f"Provider attempt status: {status}.",
                    f"Provider: {provider}.",
                ],
            )
            if status == "failed"
            else get_observability().emit(event_payload)
        )
        if event is not None:
            self._last_observability_event_id = event.event_id

        update_request(self)

    def set_result(self, result: str) -> None:
        self.result = result

    def set_error(self, error: str) -> None:
        """Store only a safe public error marker; raw details stay in logs."""
        self.error = "Internal pipeline error"

    def record_fallback(self, primary: str, fallback: str, *, success: bool, reason: str) -> None:
        event_type = "FALLBACK_SUCCESS" if success else "FALLBACK_FAILURE"
        severity = "RECOVERY" if success else "ERROR"
        event_payload = {
            "event_type": event_type,
            "severity": severity,
            "source": "backend",
            "component": "model_router",
            "summary": (
                "Model fallback completed successfully."
                if success
                else "Model fallback failed."
            ),
            "status": "completed" if success else "failed",
            "correlation_id": self.request_id,
            "parent_event_id": self._last_observability_event_id,
            "metadata": {
                "primary_provider": primary,
                "fallback_provider": fallback,
                "reason": reason,
            },
        }
        event = (
            get_observability().emit(event_payload)
            if success
            else get_observability().emit_failure(
                event_payload,
                category="MODEL_PROVIDER",
                confirmed_evidence=[
                    f"Fallback status: failed.",
                    f"Fallback provider: {fallback}.",
                ],
            )
        )
        if event is not None:
            self._last_observability_event_id = event.event_id

    def publish_complete(self) -> None:
        complete_request(self)
        event = get_observability().emit({
            "event_type": "API_REQUEST_COMPLETED",
            "severity": "INFO",
            "source": "backend",
            "component": "request_trace",
            "summary": "API request completed successfully.",
            "status": "completed",
            "correlation_id": self.request_id,
            "parent_event_id": self._last_observability_event_id,
        })
        if event is not None:
            self._last_observability_event_id = event.event_id

    def publish_failure(self) -> None:
        fail_request(self)
        event = get_observability().emit({
            "event_type": "API_REQUEST_FAILED",
            "severity": "ERROR",
            "source": "backend",
            "component": "request_trace",
            "summary": "API request failed.",
            "status": "failed",
            "correlation_id": self.request_id,
            "parent_event_id": self._last_observability_event_id,
            "metadata": {"result": self.result or "error"},
        })
        if event is not None:
            self._last_observability_event_id = event.event_id

    def to_dict(self) -> Dict:
        """Return a JSON-safe representation of this request trace."""
        return {
            "request_id": self.request_id,
            "started_at": self.started_at,
            "current_stage": self.current_stage,
            "stages": [
                {
                    "name": stage.name,
                    "status": stage.status,
                    "detail": stage.detail,
                }
                for stage in self.stages
            ],
            "selected_capability": self.selected_capability,
            "provider_attempts": [
                {
                    "provider": attempt.provider,
                    "attempt": attempt.attempt,
                    "status": attempt.status,
                    "detail": attempt.detail,
                }
                for attempt in self.provider_attempts
            ],
            "current_provider": self.current_provider,
            "current_model": self.current_model,
            "final_provider": self.final_provider,
            "final_model": self.final_model,
            "result": self.result,
            "error": self.error,
        }
