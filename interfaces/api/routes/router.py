from fastapi import APIRouter, Depends

from interfaces.api.security import require_api_auth

from interfaces.api.routes.academic import router as academic_router
from interfaces.api.routes.agents import router as agents_router
from interfaces.api.routes.capabilities import router as capabilities_router
from interfaces.api.routes.chat import router as chat_router
from interfaces.api.routes.conversations import router as conversations_router
from interfaces.api.routes.cloud import router as cloud_router
from interfaces.api.routes.cipher import router as cipher_router
from interfaces.api.routes.database import router as database_router
from interfaces.api.routes.device import router as device_router
from interfaces.api.routes.diagnostics import router as diagnostics_router
from interfaces.api.routes.health import router as health_router
from interfaces.api.routes.memory import router as memory_router
from interfaces.api.routes.learning import router as learning_router
from interfaces.api.routes.observability import router as observability_router
from interfaces.api.routes.settings import router as settings_router
from interfaces.api.routes.system_state import router as system_state_router
from interfaces.api.routes.voice import router as voice_router
from interfaces.api.routes.workspace import router as workspace_router

public_router = APIRouter(prefix="/api/v1")
public_router.include_router(health_router)

# Every route except the minimal liveness endpoint requires the Core API token.
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_auth)])
router.include_router(capabilities_router)
router.include_router(database_router)
router.include_router(chat_router)
router.include_router(conversations_router)
router.include_router(memory_router)
router.include_router(learning_router)
router.include_router(system_state_router)
router.include_router(diagnostics_router)
router.include_router(device_router)
router.include_router(observability_router)
router.include_router(agents_router)
router.include_router(academic_router)
router.include_router(workspace_router)
router.include_router(settings_router)
router.include_router(voice_router)
router.include_router(cloud_router)
router.include_router(cipher_router)
