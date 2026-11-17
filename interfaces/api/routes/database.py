"""Database and storage status API."""

from fastapi import APIRouter

from core.database_status import build_database_status
from interfaces.api.schemas.database import DatabaseStatusResponse


router = APIRouter()


@router.get(
    "/database/status",
    response_model=DatabaseStatusResponse,
)
def database_status_endpoint():
    """Return safe status information for Amiki's data stores."""
    return DatabaseStatusResponse(
        **build_database_status()
    )
