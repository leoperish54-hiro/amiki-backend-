from fastapi import APIRouter

from core.diagnostics import run_diagnostics

router = APIRouter()


@router.get("/diagnostics")
def diagnostics_endpoint():
    """Return safe, read-only runtime diagnostics."""
    return run_diagnostics()
