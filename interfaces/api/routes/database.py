"""Database and storage status API."""

from fastapi import APIRouter, Depends

from core.database_status import build_database_status
from interfaces.api.schemas.database import DatabaseStatusResponse
from interfaces.api.routes.security import require_uosa_scope

router = APIRouter()


@router.get(
    "/database/status",
    response_model=DatabaseStatusResponse,
    dependencies=[Depends(require_uosa_scope("observability.read"))],
)
def database_status_endpoint():
    """Return safe status information for Amiki's data stores."""
    return DatabaseStatusResponse(
        **build_database_status()
    )
