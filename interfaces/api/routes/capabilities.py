from fastapi import APIRouter

from core.capabilities import get_capabilities
from interfaces.api.schemas.capabilities import CapabilitiesResponse


router = APIRouter()


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities_endpoint():
    return CapabilitiesResponse(**get_capabilities())
