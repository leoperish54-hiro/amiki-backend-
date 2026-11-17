"""Read-only inspection agent adapter."""

from collections.abc import Mapping
from typing import Any

from core.agents.manager import AgentExecutionContext, AgentTool
from core.diagnostics import run_diagnostics


def _safe_projection(result: Any) -> dict[str, Any]:
    """Project diagnostics onto a bounded, internally consistent evidence contract."""
    invalid_finding = {
        "severity": "error",
        "check": "inspection",
        "finding": "Diagnostic result was invalid.",
    }
    if not isinstance(result, Mapping):
        return {
            "status": "degraded",
            "checks": [],
            "findings": [invalid_finding],
            "evidence": {"source": "runtime_diagnostic", "checks": []},
            "safe": True,
        }

    raw_checks = result.get("checks")
    raw_findings = result.get("findings")
    checks_are_valid = isinstance(raw_checks, list) and bool(raw_checks)
    safe_checks: list[dict[str, str]] = []
    if isinstance(raw_checks, list):
        for check in raw_checks:
            if not isinstance(check, Mapping):
                checks_are_valid = False
                continue
            name = check.get("name")
            check_status = check.get("status")
            detail = check.get("detail")
            if (
                not isinstance(name, str) or not name.strip()
                or check_status not in {"ok", "error"}
                or not isinstance(detail, str) or not detail.strip()
            ):
                checks_are_valid = False
                continue
            safe_checks.append({
                "name": name.strip(),
                "status": check_status,
                "detail": detail.strip(),
            })
    if len(safe_checks) != len(raw_checks) if isinstance(raw_checks, list) else True:
        checks_are_valid = False

    findings_are_valid = isinstance(raw_findings, list)
    safe_findings: list[dict[str, str]] = []
    if isinstance(raw_findings, list):
        for finding in raw_findings:
            if not isinstance(finding, Mapping):
                findings_are_valid = False
                continue
            severity = finding.get("severity")
            check = finding.get("check")
            detail = finding.get("finding")
            if (
                severity not in {"info", "warning", "error", "critical"}
                or not isinstance(check, str) or not check.strip()
                or not isinstance(detail, str) or not detail.strip()
            ):
                findings_are_valid = False
                continue
            safe_findings.append({
                "severity": severity,
                "check": check.strip(),
                "finding": detail.strip(),
            })
    if isinstance(raw_findings, list) and len(safe_findings) != len(raw_findings):
        findings_are_valid = False

    source_status = result.get("status")
    safe_marker_valid = result.get("safe") is True
    checks_failed = any(check["status"] != "ok" for check in safe_checks)
    has_error_finding = any(
        finding["severity"] in {"error", "critical"}
        for finding in safe_findings
    )
    contract_valid = (
        source_status in {"green", "degraded"}
        and safe_marker_valid
        and checks_are_valid
        and findings_are_valid
    )
    consistent_green = (
        source_status == "green"
        and not checks_failed
        and not safe_findings
    )
    status = "green" if contract_valid and consistent_green else "degraded"

    if not contract_valid:
        safe_findings = [finding for finding in safe_findings if finding != invalid_finding]
        safe_findings.append(invalid_finding)
    elif source_status == "green" and (checks_failed or safe_findings):
        # A producer must not label failed checks or findings as green.
        if not safe_findings:
            safe_findings.append({
                "severity": "error",
                "check": "inspection",
                "finding": "Diagnostic status contradicted its check evidence.",
            })
    elif checks_failed and not has_error_finding:
        failed = next(check for check in safe_checks if check["status"] != "ok")
        safe_findings.append({
            "severity": "error",
            "check": failed["name"],
            "finding": failed["detail"],
        })

    return {
        "status": status,
        "checks": safe_checks,
        "findings": safe_findings,
        "evidence": {
            "source": "runtime_diagnostic",
            "checks": [check["name"] for check in safe_checks],
        },
        "safe": True,
    }


def _build_inventory(
    context: AgentExecutionContext,
    only: set[str] | None = None,
) -> dict[str, Any]:
    """Collect a bounded inventory from live runtime registries, not guessed descriptions."""
    from core.agents.device import device_capabilities
    from core.agents.orchestrator import AgentOrchestrator
    from core.observability import get_observability
    from core.system_state import SystemState

    def capture(name: str, handler):
        context.raise_if_cancelled()
        try:
            return {"status": "observed", "data": handler()}
        except Exception:
            return {
                "status": "unavailable",
                "reason": f"The {name} inventory could not be read safely.",
            }

    def system_snapshot():
        state = SystemState.from_runtime()
        return {
            "identity": state.identity,
            "runtime": state.runtime,
            "capabilities": state.capabilities,
            "providers": {
                name: {
                    "configured": provider.configured,
                    "available": provider.available,
                    "active": provider.active,
                    "last_observed_status": provider.last_observed_status,
                    "evidence_request_id": provider.evidence_request_id,
                }
                for name, provider in state.providers.items()
            },
            "limitations": state.limitations,
        }

    def agent_snapshot():
        items = []
        for mode, builders in (
            ("orchestrated", AgentOrchestrator._builders),
            ("direct", AgentOrchestrator._direct_builders),
        ):
            for name, builder in builders.items():
                definition, manager = builder()
                items.append({
                    "agent": name,
                    "mode": mode,
                    "purpose": definition.purpose,
                    "permissions": sorted(definition.permissions),
                    "tools": [
                        {
                            "name": tool_name,
                            "permission": tool.permission,
                            "read_only": tool.read_only,
                            "timeout_seconds": tool.timeout_seconds,
                        }
                        for tool_name, tool in sorted(manager._tools.items())
                    ],
                    "max_steps": manager.max_steps,
                    "max_runtime_seconds": manager.max_runtime_seconds,
                })
        return items

    def device_snapshot():
        capabilities = device_capabilities()
        return {
            "planning_status": capabilities.get("status", "unknown"),
            "execution_model": capabilities.get("execution_model", "unknown"),
            "execution_modes": capabilities.get("execution_modes", []),
            "backend_read_only_execution": capabilities.get("backend_read_only_execution", {}),
            "backend_approved_execution": capabilities.get("backend_approved_execution", {}),
            "device_confirmation_available": capabilities.get("device_confirmation_available", False),
            "actions": [
                {
                    "name": name,
                    "user_approval": details.get("user_approval"),
                }
                for name, details in sorted(capabilities.get("actions", {}).items())
                if isinstance(details, dict)
            ],
            "bridge_connection": "not_observed",
        }

    def observability_snapshot():
        bus = get_observability()
        return {
            "operational_event_count": bus.event_store.count(),
            "permanent_diagnostic_count": len(bus.diagnostic_store.recent(500)),
            "retention_days": 7,
        }

    def orchestration_snapshot():
        return {
            "routes": [
                {"agent": agent, "keywords": list(keywords)}
                for agent, keywords in AgentOrchestrator._routes
            ],
            "orchestrated_agents": sorted(AgentOrchestrator._builders),
            "direct_agents": sorted(AgentOrchestrator._direct_builders),
        }

    def modules_snapshot():
        import importlib.util

        names = (
            "core.router",
            "core.research_learning",
            "core.learning_engine",
            "core.intelligence_diagnostics",
            "core.cloud_autonomy_worker",
            "core.goal_planner",
            "core.memory",
            "core.model",
            "core.observability",
            "core.handlers.web",
            "interfaces.api.routes.cloud",
            "interfaces.api.routes.learning",
        )
        result = {}
        for name in names:
            try:
                result[name] = {"available": importlib.util.find_spec(name) is not None}
            except (ImportError, ModuleNotFoundError, ValueError):
                result[name] = {"available": False}
        return result

    from core.intelligence_diagnostics import (
        inspect_cloud,
        inspect_core_worker_configuration,
        inspect_learning,
    )

    section_builders = {
        "system": ("system-state", system_snapshot),
        "agents": ("agent-registry", agent_snapshot),
        "device": ("device-capability", device_snapshot),
        "observability": ("observability-store", observability_snapshot),
        "orchestration": ("orchestration-registry", orchestration_snapshot),
        "modules": ("module-registry", modules_snapshot),
        "learning": ("learning-engine", inspect_learning),
        "cloud": ("cloud-control-plane", lambda: {
            "control_plane": inspect_cloud(),
            "core_worker": inspect_core_worker_configuration(),
        }),
    }
    sections = {
        key: capture(label, handler)
        for key, (label, handler) in section_builders.items()
        if only is None or key in only
    }
    unavailable = [
        name for name, section in sections.items()
        if section["status"] != "observed"
    ]
    limitations = [
        "This is a runtime inventory, not a source-code or dependency vulnerability scan.",
        "The backend can describe device action plans but cannot currently confirm that an Android bridge is connected.",
        "Provider availability reflects recorded request evidence and is not a live health probe.",
    ]
    findings = []
    if "device" in sections:
        findings.append({
            "severity": "warning",
            "check": "device_bridge",
            "finding": "Live Android device-bridge connectivity is not observable from the backend.",
        })
    for name in unavailable:
        findings.append({
            "severity": "error",
            "check": name,
            "finding": sections[name]["reason"],
        })
    return {
        "status": "degraded" if findings else "complete",
        "scope": "bounded_runtime_inventory",
        "sections": sections,
        "findings": findings,
        "limitations": limitations,
    }


def _requested_scope(message: str) -> str:
    """Resolve a direct inspection request to one bounded evidence scope."""
    lowered = " ".join(message.lower().split())
    if not lowered:
        # Tool-level callers without a message retain the explicit full-inspection
        # contract; the direct API requires a non-empty user message.
        return "full"

    full_terms = (
        "full diagnostics", "full diagnostic", "full health", "all checks",
        "everything", "system-wide", "inspect runtime", "runtime diagnostics",
        "run diagnostics", "health report", "inspect the whole system",
        "inspect the entire system",
    )
    if any(term in lowered for term in full_terms):
        return "full"

    if any(term in lowered for term in (
        "agent configuration", "agent config", "agent registry",
        "registered agents", "which agents", "agent tools", "agent permissions",
        "direct agents", "agent setup",
    )):
        return "agent_configuration"

    if any(term in lowered for term in (
        "model provider", "model providers", "provider", "openrouter",
        "gemini", "fallback provider", "provider connection", "model backend",
    )):
        return "model_providers"

    if any(term in lowered for term in (
        "orchestrator", "orchestration", "agent routing", "route requests",
        "how requests are routed",
    )):
        return "orchestration"
    if any(term in lowered for term in (
        "cloud", "goal queue", "cloud worker", "cloud schedule", "cloud job",
        "task lease", "amiki cloud",
    )):
        return "cloud"
    if any(term in lowered for term in (
        "learning", "ale", "research pipeline", "learning review",
        "review queue", "knowledge candidate", "approved knowledge",
    )):
        return "learning"
    if any(term in lowered for term in (
        "agent configuration", "agent config", "agent registry",
        "registered agents", "which agents", "agent tools", "agent permissions",
        "all agents", "every agent", "specialist agents",
    )):
        return "agent_configuration"

    if any(term in lowered for term in ("capability", "capabilities", "enabled features")):
        return "capabilities"
    if any(term in lowered for term in ("persistent memory", "memory store", "saved facts", "memory")):
        return "persistent_memory"
    if any(term in lowered for term in ("device adapter", "device bridge", "android bridge", "device action", "termux api")):
        return "device_adapter"
    if any(term in lowered for term in ("attachment extraction", "attachment", "attachments", "file import")):
        return "attachments"
    if any(term in lowered for term in ("api health", "backend health", "api status", "health endpoint")):
        return "api_health"
    if any(term in lowered for term in ("model router", "model routing", "routing configuration", "router")):
        return "model_router"
    if any(term in lowered for term in ("configuration", "config", "settings")):
        return "configuration"

    if any(term in lowered for term in (
        "inspect", "diagnostic", "diagnostics", "health check", "check status",
        "system status", "backend status", "runtime status", "amiki",
    )):
        return "full"
    if any(term in lowered for term in (
        "system", "agent", "router", "pipeline", "research", "security",
        "workspace", "university", "database", "integration", "module",
        "tool", "task", "project", "service", "api",
    )):
        return "full"
    return "help"


def _focused_message(scope: str, checks: list[dict[str, str]]) -> str:
    labels = {
        "cloud": "Amiki Cloud",
        "learning": "Amiki Learning Engine",
        "orchestration": "Agent orchestration",
        "agent_configuration": "Agent configuration",
        "model_providers": "Model providers",
        "configuration": "Runtime configuration",
        "capabilities": "Capabilities",
        "persistent_memory": "Persistent memory",
        "device_adapter": "Device adapter",
        "attachments": "Attachments",
        "api_health": "API health",
        "model_router": "Model router",
    }
    lines = [labels.get(scope, scope.replace("_", " ").title())]
    for check in checks:
        state = "OK" if check["status"] == "ok" else "ERROR"
        lines.append(f"• {state}: {check['detail']}")
    return "\n".join(lines)


def inspect_runtime(arguments, context: AgentExecutionContext):
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
                "I'm the Inspection Agent. I inspect Amiki's runtime; I don't treat "
                "ordinary chat as a system scan. Ask about model providers, agent "
                "configuration, capabilities, memory, device adapter, attachments, "
                "Cloud, learning review, agents, API health, model routing, or ask "
                "about any Amiki subsystem. Say 'full diagnostics' for a system-wide report."
            ),
            "checks": [],
            "findings": [],
            "evidence": {"source": "runtime_diagnostic", "checks": []},
        }

    if scope == "agent_configuration":
        inventory = _build_inventory(context, only={"agents"})
        section = inventory["sections"].get("agents", {})
        if section.get("status") != "observed":
            return {
                "status": "degraded",
                "scope": scope,
                "safe": True,
                "message": section.get("reason", "Agent configuration could not be read safely."),
                "findings": inventory["findings"],
                "inventory": {"agents": section},
                "evidence": {"source": "runtime_inventory", "sections": []},
            }
        agents = section["data"]
        lines = [f"Agent configuration — {len(agents)} registered agent entries"]
        for item in agents:
            tools = ", ".join(tool["name"] for tool in item.get("tools", [])) or "no tools"
            permissions = ", ".join(item.get("permissions", [])) or "no permissions"
            lines.append(f"• {item['agent']} ({item['mode']}): {item['purpose']}")
            lines.append(f"  Tools: {tools}")
            lines.append(f"  Permissions: {permissions}")
        return {
            "status": "success",
            "scope": scope,
            "safe": True,
            "message": "\n".join(lines),
            "inventory": {"agents": section},
            "findings": [],
            "evidence": {"source": "runtime_inventory", "sections": ["agents"]},
        }

    if scope == "full":
        # Cloud and ALE are collected once as structured inventory sections below.
        # The remaining checks run here, avoiding duplicate network probes.
        result = _safe_projection(run_diagnostics(only={
            "api_health", "model_providers", "configuration", "capabilities",
            "device_adapter", "persistent_memory", "attachments",
            "attachment_extraction", "model_router", "agent_registry",
            "orchestration",
        }))
        context.raise_if_cancelled()
        inventory = _build_inventory(context)
        result["scope"] = "full"
        result["inventory"] = inventory
        result["evidence"]["inventory_sections"] = [
            name for name, section in inventory["sections"].items()
            if section["status"] == "observed"
        ]
        if inventory["status"] != "complete":
            result["status"] = "degraded"
            result["findings"].extend(inventory["findings"])
        context.raise_if_cancelled()
        return result

    selected_names = {
        "cloud": {"cloud_control_plane"},
        "learning": {"learning_engine"},
        "orchestration": {"agent_registry", "orchestration"},
        "model_providers": {"model_providers"},
        "configuration": {"configuration"},
        "capabilities": {"capabilities"},
        "persistent_memory": {"persistent_memory"},
        "device_adapter": {"device_adapter"},
        "attachments": {"attachments", "attachment_extraction"},
        "api_health": {"api_health"},
        "model_router": {"model_router"},
    }[scope]
    result = run_diagnostics(only=selected_names)
    checks = [
        check for check in result.get("checks", [])
        if isinstance(check, Mapping) and check.get("name") in selected_names
    ]
    findings = [
        finding for finding in result.get("findings", [])
        if isinstance(finding, Mapping) and finding.get("check") in selected_names
    ]
    focused = _safe_projection({
        "status": "degraded" if any(check.get("status") != "ok" for check in checks) else "green",
        "safe": True,
        "checks": checks,
        "findings": findings,
    })
    focused["scope"] = scope
    focused["message"] = _focused_message(scope, focused["checks"])
    context.raise_if_cancelled()
    return focused


def build_inspection_tool() -> AgentTool:
    return AgentTool(
        handler=inspect_runtime,
        permission="agent.diagnostics.read",
        read_only=True,
        timeout_seconds=10.0,
        pass_context=True,
    )
