from fastapi import APIRouter

from interfaces.api.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok", service="amiki-api")
