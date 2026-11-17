from fastapi import APIRouter

from core.context import clear_context
from core.memory import clear_memory
from core.memory_manager import recall
from interfaces.api.schemas.memory import MemoryResponse


router = APIRouter()


@router.get("/memory", response_model=MemoryResponse)
def memory_endpoint():
    return MemoryResponse(
        memories=recall()
    )


@router.delete("/memory/current-session")
def clear_current_session_endpoint():
    """Clear the current conversation context."""
    clear_context()
    return {"status": "cleared", "target": "current_session"}


@router.delete("/memory/persistent")
def clear_persistent_memory_endpoint():
    """Clear all persistent long-term memory facts."""
    clear_memory()
    return {"status": "cleared", "target": "persistent_memory"}
