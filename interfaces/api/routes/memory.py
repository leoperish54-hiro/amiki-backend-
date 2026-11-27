from fastapi import APIRouter, Depends, HTTPException

from core.context import clear_context
from interfaces.api.routes.security import require_uosa_scope
from core.memory import (
    MemoryStoreError,
    clear_memory,
    get_memory_events,
    get_memory_history,
)
from core.memory_manager import recall
from core.memory_consolidator import review_memory_clusters
from interfaces.api.schemas.memory import MemoryHistoryResponse, MemoryResponse


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



@router.get(
    "/memory/history",
    response_model=MemoryHistoryResponse,
    dependencies=[Depends(require_uosa_scope("memory.read"))],
)
def memory_history_endpoint(category: str | None = None, key: str | None = None, limit: int = 100):
    """Return immutable temporal fact history and its audit events."""
    try:
        return MemoryHistoryResponse(
            history=get_memory_history(category=category, key=key, limit=limit),
            events=get_memory_events(category=category, key=key, limit=limit),
        )
    except (MemoryStoreError, ValueError):
        raise HTTPException(
            status_code=503,
            detail="Persistent memory history is unavailable.",
        ) from None


@router.get(
    "/memory/consolidation",
    dependencies=[Depends(require_uosa_scope("memory.read"))],
)
def memory_consolidation_endpoint():
    """Preview duplicate, related and contradictory memories without mutating them."""
    try:
        return review_memory_clusters()
    except MemoryStoreError:
        raise HTTPException(
            status_code=503,
            detail="Persistent memory analysis is unavailable.",
        ) from None
