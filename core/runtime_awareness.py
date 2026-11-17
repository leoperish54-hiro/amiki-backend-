"""Runtime awareness state for Amiki."""

from copy import deepcopy
from datetime import datetime, timezone
import re
from threading import Lock
from typing import Dict, Optional


_LOCK = Lock()

_STATE = {
    "state": "idle",
    "current_request": None,
    "current_stage": None,
    "current_capability": None,
    "current_provider": None,
    "current_model": None,
    "last_request": None,
    "provider_observations": {},
}


def begin_request(trace) -> None:
    """Publish the beginning of a request."""
    with _LOCK:
        _STATE["state"] = "processing"
        _STATE["current_request"] = trace.request_id
        _STATE["current_stage"] = trace.current_stage
        _STATE["current_capability"] = trace.selected_capability
        _STATE["current_provider"] = trace.current_provider
        _STATE["current_model"] = trace.current_model


def update_request(trace) -> None:
    """Publish the latest observable state of a request."""
    with _LOCK:
        _STATE["state"] = "processing"
        _STATE["current_request"] = trace.request_id
        _STATE["current_stage"] = trace.current_stage
        _STATE["current_capability"] = trace.selected_capability
        _STATE["current_provider"] = trace.current_provider
        _STATE["current_model"] = trace.current_model


def complete_request(trace) -> None:
    """Publish a completed request and its final telemetry."""
    with _LOCK:
        _STATE["state"] = (
            "error" if trace.error or trace.result == "error"
            else "idle"
        )
        _STATE["current_request"] = None
        _STATE["current_stage"] = None
        _STATE["current_capability"] = None
        _STATE["current_provider"] = None
        _STATE["current_model"] = None
        _STATE["last_request"] = deepcopy(trace.to_dict())


def fail_request(trace) -> None:
    """Publish a failed request and its final telemetry."""
    with _LOCK:
        _STATE["state"] = "error"
        _STATE["current_request"] = None
        _STATE["current_stage"] = None
        _STATE["current_capability"] = None
        _STATE["current_provider"] = None
        _STATE["current_model"] = None
        _STATE["last_request"] = deepcopy(trace.to_dict())


def _safe_provider_detail(detail: Optional[str]) -> Optional[str]:
    """Keep only diagnostic categories; never retain arbitrary provider text."""
    if not isinstance(detail, str):
        return None
    value = detail.strip()
    match = re.match(r"(?i)^HTTP\s+(\d{3})\b", value)
    if match:
        return "HTTP " + match.group(1)
    lowered = value.lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "connection error" in lowered or "connection failed" in lowered:
        return "connection error"
    return None


def record_provider_observation(
    provider: str,
    status: str,
    *,
    request_id: Optional[str] = None,
    model: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    """Retain the latest observed provider outcome across subsequent requests."""
    if provider not in {"gemini", "openrouter"} or status not in {"success", "failed"}:
        return

    observation = {
        "status": status,
        "available": status == "success",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id if isinstance(request_id, str) else None,
        "model": model if isinstance(model, str) else None,
        "detail": _safe_provider_detail(detail),
    }
    with _LOCK:
        _STATE.setdefault("provider_observations", {})[provider] = observation


def get_runtime_awareness() -> Dict:
    """Return a safe copy of the current runtime awareness state."""
    with _LOCK:
        return deepcopy(_STATE)
