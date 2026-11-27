from fastapi import APIRouter, Depends, HTTPException

from core.context import clear_context
from interfaces.api.routes.security import require_uosa_scope
from core.memory import MemoryStoreError, clear_memory
from core.memory_manager import recall
from interfaces.api.schemas.memory import MemoryResponse


router = APIRouter()


@router.get("/memory", response_model=MemoryResponse, dependencies=[Depends(require_uosa_scope("memory.read"))])
def memory_endpoint():
    try:
        memories = recall()
    except MemoryStoreError:
        raise HTTPException(
            status_code=503,
            detail="Persistent memory is unavailable. No data was changed.",
        ) from None
    return MemoryResponse(memories=memories)


@router.delete("/memory/current-session", dependencies=[Depends(require_uosa_scope("memory.session.clear"))])
def clear_current_session_endpoint():
    """Clear the current conversation context."""
    clear_context()
    return {"status": "cleared", "target": "current_session"}


@router.delete("/memory/persistent", dependencies=[Depends(require_uosa_scope("memory.persistent.clear"))])
def clear_persistent_memory_endpoint():
    """Clear all persistent long-term memory facts."""
    clear_memory()
    return {"status": "cleared", "target": "persistent_memory"}
