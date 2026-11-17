"""Safe runtime diagnostics for Amiki.

Diagnostics are intentionally read-only and never expose secrets, model payloads,
or raw exception details.
"""

import importlib
import json
from urllib.error import URLError
from urllib.request import Request, urlopen
from typing import Any, Callable

_HEALTH_URL = "http://127.0.0.1:8000/api/v1/health"
_HEALTH_TIMEOUT_SECONDS = 2.0


class DiagnosticIssue(RuntimeError):
    """Safe, user-actionable diagnostic finding without sensitive details."""


from core.capabilities import get_capabilities
from core.system_state import SystemState


def _check(name: str, fn: Callable[[], str]) -> dict[str, Any]:
    try:
        detail = fn()
        return {"name": name, "status": "ok", "detail": detail}
    except DiagnosticIssue as error:
        return {"name": name, "status": "error", "detail": str(error)}
    except Exception:
        return {
            "name": name,
            "status": "error",
            "detail": "Check failed. See backend logs for developer diagnostics.",
        }


def _api_health_check() -> str:
    """Verify the actual local API health contract without exposing internals."""
    request = Request(
        _HEALTH_URL,
        headers={"User-Agent": "Amiki-Diagnostics/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=_HEALTH_TIMEOUT_SECONDS) as response:
            status_code = getattr(response, "status", None)
            payload = json.loads(response.read(65536).decode("utf-8"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        raise RuntimeError("API health check failed.") from None
    if status_code != 200 or not isinstance(payload, dict):
        raise RuntimeError("API health check failed.") from None
    if payload.get("status") != "ok" or payload.get("service") != "amiki-api":
        raise RuntimeError("API health check failed.") from None
    return "HTTP 200; service=amiki-api"


def _cloud_check() -> str:
    from core.intelligence_diagnostics import inspect_cloud

    state = inspect_cloud()
    return (
        "Cloud health and readiness observed; "
        f"jobs={state.get('jobs_count')}, goals={state.get('goals_count')}, "
        f"worker={state.get('worker_status')} (enabled={state.get('worker_enabled')}), "
        f"queued={state.get('queued_jobs')}, running={state.get('running_jobs')}, "
        f"schedules={state.get('schedules_count')}, "
        f"schedule_api={state.get('schedule_api_status')}."
    )


def _learning_check() -> str:
    from core.intelligence_diagnostics import inspect_learning

    state = inspect_learning()
    return (
        f"ALE enabled={state['enabled']}; pending knowledge={state['pending_knowledge']}; "
        f"pending experiences={state['pending_experiences']}; "
        f"approved knowledge={state['approved_knowledge']}; "
        f"active skills={state['active_skills']}; model weights updated="
        f"{state['model_weights_updated']}."
    )


def _agent_registry_check() -> str:
    from core.agents.orchestrator import AgentOrchestrator

    orchestrated = sorted(AgentOrchestrator._builders)
    direct = sorted(AgentOrchestrator._direct_builders)
    if not orchestrated:
        raise DiagnosticIssue("No orchestrated specialist agents are registered.")
    return (
        f"{len(orchestrated)} orchestrated and {len(direct)} direct agent registrations: "
        + ", ".join(orchestrated)
        + "."
    )


def _orchestration_check() -> str:
    from core.agents.orchestrator import AgentOrchestrator

    routes = AgentOrchestrator._routes
    routed_agents = {agent for agent, _keywords in routes}
    missing = sorted(routed_agents - set(AgentOrchestrator._builders))
    if missing:
        raise DiagnosticIssue(
            "Orchestration routes reference unregistered agents: " + ", ".join(missing) + "."
        )
    return f"{len(routes)} deterministic routing groups reference registered agents."


def run_diagnostics(only: set[str] | None = None) -> dict[str, Any]:
    """Run bounded, read-only checks; optionally restrict work to named checks."""
    check_specs: list[tuple[str, Callable[[], str]]] = [
        ("api_health", _api_health_check),
        ("cloud_control_plane", _cloud_check),
        ("learning_engine", _learning_check),
        ("agent_registry", _agent_registry_check),
        ("orchestration", _orchestration_check),
        ("model_providers", _provider_check),
        ("configuration", lambda: "loaded" if SystemState.from_runtime() else "unavailable"),
        ("capabilities", _capabilities_check),
        ("device_adapter", _device_adapter_check),
        ("persistent_memory", _memory_check),
        ("attachments", lambda: _module_check("core.attachments")),
        ("attachment_extraction", lambda: _module_check("core.attachment_extraction")),
        ("model_router", lambda: _module_check("core.model")),
    ]
    checks = [
        _check(name, handler)
        for name, handler in check_specs
        if only is None or name in only
    ]

    findings = [
        {
            "severity": "error",
            "check": check["name"],
            "finding": check["detail"],
        }
        for check in checks
        if check["status"] != "ok"
    ]

    return {
        "status": "green" if not findings else "degraded",
        "checks": checks,
        "findings": findings,
        "safe": True,
    }


def _provider_check() -> str:
    """Report actionable provider configuration/runtime hints without secrets."""
    import os

    from core.runtime_awareness import _safe_provider_detail, get_runtime_awareness

    configured = {
        "gemini": bool(os.getenv("GEMINI_API_KEY")),
        "openrouter": bool(os.getenv("OPENROUTER_API_KEY")),
    }

    missing = [
        provider for provider, is_configured in configured.items()
        if not is_configured
    ]
    if missing:
        raise DiagnosticIssue(
            "Provider configuration incomplete: "
            + ", ".join(missing)
            + " API key not configured."
        )

    runtime = get_runtime_awareness()
    latest_by_provider = {}
    observations = runtime.get("provider_observations")
    if isinstance(observations, dict):
        for provider_name, observation in observations.items():
            if (
                provider_name in configured
                and isinstance(observation, dict)
                and observation.get("status") in {"success", "failed"}
            ):
                latest_by_provider[provider_name] = {
                    "provider": provider_name,
                    "status": observation.get("status"),
                    "detail": observation.get("detail") or observation.get("status"),
                    "observed_at": observation.get("observed_at") or "",
                }

    # Backward-compatible fallback for traces created before retained
    # provider observations were added.
    if not latest_by_provider:
        last_request = runtime.get("last_request") or {}
        attempts = last_request.get("provider_attempts") or []
        if isinstance(attempts, list):
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                provider_name = attempt.get("provider")
                status = attempt.get("status")
                if provider_name in configured and status in {"success", "failed"}:
                    latest_by_provider[provider_name] = {
                        **attempt,
                        "provider": provider_name,
                        "status": status,
                        "detail": _safe_provider_detail(attempt.get("detail")),
                        "observed_at": last_request.get("started_at") or "",
                    }

    if not latest_by_provider:
        return (
            "Gemini and OpenRouter are configured; availability is unknown "
            "until provider requests provide evidence."
        )

    failed = [
        attempt for attempt in latest_by_provider.values()
        if attempt.get("status") == "failed"
    ]
    if not failed:
        observed = ", ".join(sorted(latest_by_provider))
        unobserved = sorted(set(configured) - set(latest_by_provider))
        detail = "Latest observed provider attempts succeeded for: " + observed + "."
        if unobserved:
            detail += " Availability remains unknown for: " + ", ".join(unobserved) + "."
        return detail + " No live health probe was performed."

    latest = max(
        failed,
        key=lambda attempt: str(attempt.get("observed_at", "")),
    )
    provider = latest.get("provider", "provider")
    detail = latest.get("detail") or "unknown failure"

    hints = {
        "HTTP 401": "authentication rejected; verify the provider API key.",
        "HTTP 403": "access forbidden; verify API key permissions/project access.",
        "HTTP 402": "account or credit limit reached.",
        "HTTP 429": "rate limited; retry later or use the fallback provider.",
        "HTTP 500": "provider server error; fallback is appropriate.",
        "HTTP 502": "provider/upstream gateway error; fallback is appropriate.",
        "HTTP 503": "provider temporarily unavailable; fallback is appropriate.",
        "HTTP 504": "provider/upstream timeout; fallback is appropriate.",
        "timeout": "request timed out; fallback is appropriate.",
        "connection error": "connection failed; check network access.",
    }
    hint = hints.get(detail, "provider request failed; inspect the provider status.")
    raise DiagnosticIssue(f"{provider} {detail}: {hint}")


def _device_adapter_check() -> str:
    """Verify that Termux has the commands needed for UI-independent device work."""
    from core.handlers.termux_device import termux_device_capabilities

    capabilities = termux_device_capabilities()
    read_only = capabilities.get("actions", {})
    approved = capabilities.get("approved_actions", {})
    available_read_only = sorted(
        name for name, details in read_only.items()
        if isinstance(details, dict) and details.get("available") is True
    )
    available_approved = sorted(
        name for name, details in approved.items()
        if isinstance(details, dict) and details.get("available") is True
    )

    if not capabilities.get("confirmation_dialog_available"):
        raise DiagnosticIssue(
            "Termux:API confirmation dialog is unavailable; UI-independent device actions are disabled."
        )
    if not available_read_only:
        raise DiagnosticIssue(
            "No Termux:API read-only device query is available; install the Termux:API app and termux-api package."
        )
    if not available_approved:
        raise DiagnosticIssue(
            "No confirmation-gated Termux device action is available; install the required Termux:API commands."
        )

    return (
        f"{len(available_read_only)} read-only and {len(available_approved)} "
        "confirmation-gated device actions detected: "
        + ", ".join(available_approved)
        + ". Command availability does not prove Android permissions or final execution."
    )


def _memory_check() -> str:
    from core.memory import load_memory

    memory = load_memory()
    if not isinstance(memory, dict):
        raise DiagnosticIssue("Persistent memory has an invalid structure.")
    facts = memory.get("facts")
    if not isinstance(facts, list):
        raise DiagnosticIssue("Persistent memory facts must be stored as a list.")
    return f"{len(facts)} persistent facts"


def _capabilities_check() -> str:
    """Verify required capabilities are present and explicitly enabled."""
    capabilities = get_capabilities()
    required = {
        "analysis",
        "university",
        "workspace",
        "noctis_requiem",
        "web",
        "device",
    }
    unavailable = sorted(
        name for name in required if capabilities.get(name) is not True
    )
    if unavailable:
        raise DiagnosticIssue(
            "Required capabilities are missing or disabled: "
            + ", ".join(unavailable)
            + "."
        )
    return f"All {len(required)} required capabilities are enabled."


def _module_check(module_name: str) -> str:
    importlib.import_module(module_name)
    return "importable"
