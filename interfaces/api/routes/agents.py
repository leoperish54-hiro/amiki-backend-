"""Bounded direct specialist-agent API for Android and other trusted clients."""

from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.agents.orchestrator import AgentOrchestrator
from core.agents.specialized import (
    build_analysis_agent,
    build_direct_analysis_agent,
    build_direct_tutor_agent,
    build_engineering_agent,
    build_direct_engineering_agent,
    build_inspection_agent,
    build_diagnostics_agent,
    build_research_agent,
    build_tutor_agent,
    build_security_agent,
)

router = APIRouter()

_DIRECT_BUILDERS = {
    "research": build_research_agent,
    "tutor": build_direct_tutor_agent,
    "analysis": build_direct_analysis_agent,
    "engineering": build_direct_engineering_agent,
    "inspection": build_inspection_agent,
    "diagnostics": build_diagnostics_agent,
    "security": build_security_agent,
}

_CONTEXT_KEYS = {
    "research": frozenset({"topic", "sources", "goal"}),
    "tutor": frozenset({"subject", "level", "topic", "goal"}),
    "analysis": frozenset({"subject", "task_type", "assumptions"}),
    "engineering": frozenset({"project", "project_id", "source_paths", "goal"}),
    "inspection": frozenset({"focus", "expected_state"}),
    "diagnostics": frozenset(),
    "security": frozenset({"focus", "scope"}),
}


class AgentDirectRequest(BaseModel):
    agent: Literal["research", "tutor", "analysis", "engineering", "inspection", "diagnostics", "security"]
    input: str = Field(min_length=1, max_length=4000)
    context: dict = Field(default_factory=dict)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("/agents/capabilities")
def direct_agent_capabilities():
    agents = []
    for name, builder in _DIRECT_BUILDERS.items():
        definition, manager = builder()
        agents.append({
            "agent": definition.name,
            "purpose": definition.purpose,
            "permissions": sorted(definition.permissions),
            "tools": sorted(definition.tools),
            "direct_access": True,
            "read_only": all(tool.read_only for tool in manager._tools.values()),
            "max_steps": manager.max_steps,
            "max_runtime_seconds": manager.max_runtime_seconds,
        })
    return {
        "capability": "direct_agents",
        "version": "v1",
        "status": "ready",
        "agents": agents,
    }


@router.post("/agents/run")
def run_direct_agent(request: AgentDirectRequest):
    allowed_context = _CONTEXT_KEYS[request.agent]
    if any(key not in allowed_context for key in request.context):
        raise HTTPException(status_code=400, detail="Agent context contains unsupported fields.")
    if any(not isinstance(value, (str, list)) for value in request.context.values()):
        raise HTTPException(status_code=400, detail="Agent context values must be text or text lists.")
    for value in request.context.values():
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            raise HTTPException(status_code=400, detail="Agent context lists must contain text values.")
    if len(str(request.context)) > 2000:
        raise HTTPException(status_code=400, detail="Agent context exceeds the size limit.")

    context_lines = []
    for key in sorted(request.context):
        value = request.context[key]
        rendered = ", ".join(value) if isinstance(value, list) else value
        context_lines.append(f"{key.replace('_', ' ').title()}: {rendered}")
    agent_input = request.input.strip()
    if context_lines:
        agent_input = "Agent-specific context:\n" + "\n".join(context_lines) + "\n\nRequest:\n" + agent_input

    orchestrator = AgentOrchestrator()
    correlation_id = f"direct:{request.agent}:{uuid4().hex}"
    try:
        result = orchestrator.execute_for_agent(
            request.agent,
            agent_input,
            correlation_id=correlation_id,
            direct=True,
            project_id=request.context.get("project_id") if request.agent == "engineering" else None,
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="The requested agent could not complete safely.",
        ) from None

    if result.status != "success":
        raise HTTPException(
            status_code=503,
            detail="The requested agent could not complete safely.",
        )

    results = result.results
    # Inspection intentionally returns a safe structured projection. Normalize
    # that projection under the shared `data` envelope expected by direct clients,
    # matching the result shape already used by the other specialist agents.
    if request.agent == "inspection":
        results = [
            {"data": item} if isinstance(item, dict) and "data" not in item else item
            for item in results
        ]

    return {
        "capability": "direct_agent",
        "status": "success",
        "agent": request.agent,
        "provenance": {
            "agent": request.agent,
            "mode": "direct",
            "orchestrator": False,
        },
        "conversation_id": request.conversation_id,
        "session_id": request.session_id,
        "context_keys": sorted(request.context),
        "completed_steps": result.completed_steps,
        "planned_steps": result.planned_steps,
        "stop_condition": result.stop_condition,
        "results": results,
        "evidence": result.evidence,
    }


__all__ = ["router"]