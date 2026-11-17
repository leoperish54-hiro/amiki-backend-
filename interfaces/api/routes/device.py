from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.agents.device import device_capabilities, plan_device_action, plan_device_request
from interfaces.api.routes.security import require_uosa_scope
from core.handlers.termux_device import (
    execute_approved_device_action,
    read_device_action,
)

router = APIRouter()


class DevicePlanRequest(BaseModel):
    action: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)


class DeviceMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.get("/device/capabilities")
def device_capabilities_endpoint():
    return device_capabilities()


@router.get("/device/state", dependencies=[Depends(require_uosa_scope("device.read"))])
def device_state_endpoint():
    """Read bounded device state through Termux:API without the Amiki UI."""
    return {
        "capability": "device",
        **read_device_action("device_state"),
    }


@router.post("/device/execute", dependencies=[Depends(require_uosa_scope("device.execute"))])
def device_execute_endpoint(request: DevicePlanRequest):
    """Execute a supported action only after a Termux:API confirmation prompt."""
    return {
        "capability": "device",
        **execute_approved_device_action(request.action, request.arguments),
    }


@router.post("/device/plan", dependencies=[Depends(require_uosa_scope("device.plan"))])
def device_plan_endpoint(request: DevicePlanRequest):
    try:
        plan = plan_device_action(request.action, request.arguments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "capability": "device",
        "status": "planned",
        "action": plan.action,
        "arguments": plan.arguments,
        "requires_user_approval": plan.requires_user_approval,
        "reason": plan.reason,
        "execution_model": "android_client",
    }

@router.post("/device/plan-request", dependencies=[Depends(require_uosa_scope("device.plan"))])
def device_plan_request_endpoint(request: DeviceMessageRequest):
    try:
        plan = plan_device_request(request.message)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "capability": "device",
        "status": "planned",
        "action": plan.action,
        "arguments": plan.arguments,
        "requires_user_approval": plan.requires_user_approval,
        "reason": plan.reason,
        "execution_model": "android_client",
    }
