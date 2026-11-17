from fastapi import APIRouter, Depends

from core.diagnostics import run_diagnostics

from interfaces.api.routes.security import require_uosa_scope

router = APIRouter()


@router.get("/diagnostics", dependencies=[Depends(require_uosa_scope("observability.read"))])
def diagnostics_endpoint():
    """Return safe, read-only runtime diagnostics."""
    return run_diagnostics()
