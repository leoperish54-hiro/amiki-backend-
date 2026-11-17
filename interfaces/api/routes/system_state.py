from fastapi import APIRouter

from core.system_state import SystemState
from core.awareness import build_awareness_snapshot
from core.runtime_awareness import get_runtime_awareness
from interfaces.api.schemas.system_state import SystemStateResponse

router = APIRouter()


@router.get("/system-state", response_model=SystemStateResponse)
def system_state_endpoint():
    state = SystemState.from_runtime()
    runtime_awareness = get_runtime_awareness()
    awareness = build_awareness_snapshot(runtime_awareness)

    return SystemStateResponse(
        identity=state.identity,
        runtime=state.runtime,
        capabilities=state.capabilities,
        device=state.device,
        providers={
            name: {
                "configured": provider.configured,
                "available": provider.available,
                "active": provider.active,
                "last_observed_status": provider.last_observed_status,
                "evidence_request_started_at": provider.evidence_request_started_at,
                "evidence_request_id": provider.evidence_request_id,
            }
            for name, provider in state.providers.items()
        },
        architecture=state.architecture,
        context=state.context,
        limitations=state.limitations,
        awareness=awareness,
    )
