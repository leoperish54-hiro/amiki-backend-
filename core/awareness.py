"""Request-scoped system awareness for Amiki."""

from typing import Dict

from core.request_trace import RequestTrace


def build_awareness_snapshot(awareness: Dict) -> Dict:
    """Build the public awareness contract from runtime awareness state."""
    return {
        "state": awareness["state"],
        "current_request": awareness["current_request"],
        "current_stage": awareness["current_stage"],
        "current_capability": awareness["current_capability"],
        "current_provider": awareness["current_provider"],
        "current_model": awareness["current_model"],
        "last_request": awareness["last_request"],
    }


def build_reasoning_awareness(trace: RequestTrace) -> Dict:
    """Build the safe system awareness contract available to reasoning."""
    from core.capabilities import get_capabilities
    from core.runtime_awareness import get_runtime_awareness

    runtime = get_runtime_awareness()

    return {
        "state": runtime["state"],
        "current_stage": runtime["current_stage"],
        "current_capability": runtime["current_capability"],
        "selected_capability": trace.selected_capability,
        "capabilities": get_capabilities(),
    }
