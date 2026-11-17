"""Dedicated read-only Diagnostics Agent for Amiki-wide health investigations."""
from __future__ import annotations

from collections.abc import Mapping

from core.agents.inspection import _focused_message, _requested_scope, _safe_projection
from core.agents.manager import AgentExecutionContext, AgentTool
from core.diagnostics import run_diagnostics


_SCOPE_CHECKS = {
    "cloud": {"cloud_control_plane"},
    "learning": {"learning_engine"},
    "orchestration": {"agent_registry", "orchestration"},
    "agent_configuration": {"agent_registry", "orchestration"},
    "model_providers": {"model_providers"},
    "configuration": {"configuration"},
    "capabilities": {"capabilities"},
    "persistent_memory": {"persistent_memory"},
    "device_adapter": {"device_adapter"},
    "attachments": {"attachments", "attachment_extraction"},
    "api_health": {"api_health"},
    "model_router": {"model_router"},
}


def diagnose_runtime(arguments, context: AgentExecutionContext) -> dict:
    """Run all or a focused set of read-only diagnostics with evidence."""
    context.raise_if_cancelled()
    message = ""
    if isinstance(arguments, Mapping):
        candidate = arguments.get("message", "")
        if isinstance(candidate, str):
            message = candidate.strip()

    scope = _requested_scope(message)
    if scope == "help":
        return {
            "status": "ready",
            "scope": "help",
            "safe": True,
            "message": (
                "I'm the Diagnostics Agent. Ask me to run diagnostics, check Cloud, "
                "review ALE health, inspect agent routing, or investigate a specific "
                "Amiki subsystem. I test state; I do not repair or mutate it."
            ),
            "checks": [],
            "findings": [],
            "evidence": {"source": "runtime_diagnostic", "checks": []},
        }

    context.raise_if_cancelled()
    selected = None if scope == "full" else _SCOPE_CHECKS.get(scope)
    if selected is None and scope != "full":
        selected = {"agent_registry", "orchestration"}
    result = _safe_projection(run_diagnostics(only=selected))
    result["scope"] = scope
    result["message"] = _focused_message(scope, result["checks"])
    context.raise_if_cancelled()
    return result


def build_diagnostics_tool() -> AgentTool:
    return AgentTool(
        handler=diagnose_runtime,
        permission="agent.diagnostics.read",
        read_only=True,
        timeout_seconds=10.0,
        pass_context=True,
    )
