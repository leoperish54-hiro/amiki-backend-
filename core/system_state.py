"""Runtime system awareness for Amiki."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from core.capabilities import get_capabilities
from core.handlers.termux_device import termux_device_capabilities
from core.runtime_awareness import get_runtime_awareness


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

load_dotenv()


@dataclass
class ProviderState:
    """Safe runtime information about an AI provider."""

    configured: bool = False
    available: Optional[bool] = None
    active: bool = False
    last_observed_status: Optional[str] = None
    evidence_request_started_at: Optional[str] = None
    evidence_request_id: Optional[str] = None


def _provider_observation(runtime_awareness: Dict[str, Any], provider: str):
    """Return the latest observed outcome, including evidence from earlier requests."""
    observations = runtime_awareness.get("provider_observations")
    observation = observations.get(provider) if isinstance(observations, dict) else None
    if isinstance(observation, dict) and observation.get("status") in {"success", "failed"}:
        status = observation["status"]
        observed_at = observation.get("observed_at")
        request_id = observation.get("request_id")
        return (
            status == "success",
            status,
            observed_at if isinstance(observed_at, str) else None,
            request_id if isinstance(request_id, str) else None,
        )

    last_request = runtime_awareness.get("last_request")
    if not isinstance(last_request, dict):
        return None, None, None, None

    attempts = last_request.get("provider_attempts")
    if not isinstance(attempts, list):
        return None, None, None, None

    for attempt in reversed(attempts):
        if not isinstance(attempt, dict) or attempt.get("provider") != provider:
            continue
        status = attempt.get("status")
        if status not in {"success", "failed"}:
            continue
        available = status == "success"
        observed_at = last_request.get("started_at")
        request_id = last_request.get("request_id")
        return (
            available,
            status,
            observed_at if isinstance(observed_at, str) else None,
            request_id if isinstance(request_id, str) else None,
        )
    return None, None, None, None


@dataclass
class SystemState:
    """Observable runtime state of the Amiki system."""

    identity: Dict[str, str] = field(default_factory=dict)

    runtime: Dict[str, str] = field(
        default_factory=lambda: {
            "api_status": "ok",
            "state": "idle",
        }
    )

    capabilities: Dict[str, bool] = field(default_factory=dict)

    device: Dict[str, Any] = field(default_factory=dict)

    providers: Dict[str, ProviderState] = field(
        default_factory=lambda: {
            "gemini": ProviderState(),
            "openrouter": ProviderState(),
        }
    )

    architecture: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "components": [
                "Android Interface",
                "API Layer",
                "Router",
                "Memory",
                "Capabilities",
                "Workspace",
                "Model Router",
                "Gemini",
                "OpenRouter",
                "System Awareness",
            ]
        }
    )

    context: Dict[str, Optional[str]] = field(
        default_factory=lambda: {
            "active_capability": None,
        }
    )

    limitations: List[str] = field(default_factory=list)

    @classmethod
    def from_runtime(cls) -> "SystemState":
        """Build a safe snapshot from Amiki's current configuration."""

        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            config = json.load(file)

        assistant = config.get("assistant", {})
        identity_config = config.get("identity", {})

        runtime_awareness = get_runtime_awareness()

        provider_observations = {
            name: _provider_observation(runtime_awareness, name)
            for name in ("gemini", "openrouter")
        }

        termux_capabilities = termux_device_capabilities()
        read_only_actions = termux_capabilities.get("actions", {})
        approved_actions = termux_capabilities.get("approved_actions", {})
        available_read_only = sorted(
            name for name, details in read_only_actions.items()
            if isinstance(details, dict) and details.get("available") is True
        )
        available_approved = sorted(
            name for name, details in approved_actions.items()
            if isinstance(details, dict) and details.get("available") is True
        )
        device_status = (
            "available"
            if termux_capabilities.get("confirmation_dialog_available") and available_approved
            else "partial"
            if available_read_only or available_approved
            else "unavailable"
        )
        device_state = {
            "status": device_status,
            "adapter": termux_capabilities.get("adapter", "unknown"),
            "read_only_actions": {
                name: details.get("available") is True
                for name, details in read_only_actions.items()
                if isinstance(details, dict)
            },
            "confirmed_actions": {
                name: details.get("available") is True
                for name, details in approved_actions.items()
                if isinstance(details, dict)
            },
            "confirmation_dialog_available": bool(
                termux_capabilities.get("confirmation_dialog_available")
            ),
            "bridge_connection": "not_observed",
        }

        return cls(
            identity={
                "name": assistant.get("name", "Amiki"),
                "version": assistant.get("version", "unknown"),
                "role": identity_config.get(
                    "role",
                    "Personal AI assistant and digital companion",
                ),
            },
            runtime={
                "api_status": "ok",
                "state": runtime_awareness["state"],
            },
            capabilities=get_capabilities(),
            device=device_state,
            providers={
                "gemini": ProviderState(
                    configured=bool(os.getenv("GEMINI_API_KEY")),
                    available=provider_observations["gemini"][0],
                    active=(runtime_awareness["current_provider"] == "gemini"),
                    last_observed_status=provider_observations["gemini"][1],
                    evidence_request_started_at=provider_observations["gemini"][2],
                    evidence_request_id=provider_observations["gemini"][3],
                ),
                "openrouter": ProviderState(
                    configured=bool(os.getenv("OPENROUTER_API_KEY")),
                    available=provider_observations["openrouter"][0],
                    active=(runtime_awareness["current_provider"] == "openrouter"),
                    last_observed_status=provider_observations["openrouter"][1],
                    evidence_request_started_at=provider_observations["openrouter"][2],
                    evidence_request_id=provider_observations["openrouter"][3],
                ),
            },
            context={
                "active_capability": runtime_awareness["current_capability"],
                "current_provider": runtime_awareness["current_provider"],
                "current_model": runtime_awareness["current_model"],
                "last_provider": (
                    runtime_awareness["last_request"].get("final_provider")
                    if runtime_awareness["last_request"]
                    else None
                ),
                "last_model": (
                    runtime_awareness["last_request"].get("final_model")
                    if runtime_awareness["last_request"]
                    else None
                ),
            },
            limitations=[
                "Provider availability reflects the latest observed outcome retained in backend process memory, not a live provider health probe; observations reset when the backend restarts.",
                "Device command availability reflects installed commands, not proof of granted Android permissions or successful execution.",
                "The backend does not maintain a persistent Android bridge heartbeat; device capabilities are checked on demand.",
            ],
        )
