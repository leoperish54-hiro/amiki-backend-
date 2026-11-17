"""Safe runtime settings API for the Android companion UI.

Only user-facing configuration is returned. Provider credentials, filesystem paths,
and other secret-bearing values are deliberately excluded.
"""
import json
import os
import re
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import context as context_runtime
from interfaces.api.routes.security import require_uosa_scope
from core import model as model_runtime
from core.system_state import BASE_DIR

router = APIRouter()
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class ModelSettingsUpdate(BaseModel):
    model: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$",
    )
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)


class MemorySettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    max_messages: Optional[int] = Field(default=None, ge=1, le=200)


def _read_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=503,
            detail="Amiki settings could not be read safely.",
        ) from error
    if not isinstance(config, dict):
        raise HTTPException(
            status_code=503,
            detail="Amiki settings have an invalid format.",
        )
    return config


def _write_config(config: dict) -> None:
    """Atomically persist configuration so a partial write cannot corrupt it."""
    directory = os.path.dirname(CONFIG_FILE) or "."
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".config.",
            suffix=".tmp",
            dir=directory,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file, indent=2, ensure_ascii=False)
            config_file.write("\n")
            config_file.flush()
            os.fsync(config_file.fileno())
        os.replace(temporary_path, CONFIG_FILE)
    except OSError as error:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
        raise HTTPException(
            status_code=503,
            detail="Amiki settings could not be saved.",
        ) from error


def _display_user_name(config: dict) -> str:
    relationship = (
        config.get("persona", {}).get("relationship", {})
        if isinstance(config.get("persona"), dict)
        else {}
    )
    identity = config.get("identity", {})
    value = relationship.get("user") if isinstance(relationship, dict) else None
    if not isinstance(value, str) or not value.strip():
        value = identity.get("creator", "") if isinstance(identity, dict) else ""
    value = value.strip() if isinstance(value, str) else ""
    # Prefer a configured nickname in parentheses, when one is supplied.
    match = re.search(r"\(([^()]+)\)", value)
    return match.group(1).strip() if match else value


@router.get("/settings", dependencies=[Depends(require_uosa_scope("settings.read"))])
def settings_endpoint():
    """Return live settings and identity data for clients; never return credentials."""
    config = _read_config()
    assistant = config.get("assistant", {})
    identity = config.get("identity", {})
    model_config = config.get("model", {})
    memory_config = config.get("memory", {})

    return {
        "profile": {
            "assistant_name": assistant.get("name", ""),
            "version": assistant.get("version", ""),
            "role": identity.get("role", ""),
            "user_name": _display_user_name(config),
            "purpose": identity.get("purpose", ""),
        },
        "model": {
            "provider": model_config.get("provider", ""),
            "name": model_runtime.MODEL,
            "temperature": model_runtime.TEMPERATURE,
        },
        "memory": {
            "enabled": bool(context_runtime.MEMORY_ENABLED),
            "max_messages": int(context_runtime.MAX_MESSAGES),
        },
        "providers": {
            "gemini": {"configured": bool(model_runtime.GEMINI_API_KEY)},
            "openrouter": {"configured": bool(model_runtime.OPENROUTER_API_KEY)},
        },
    }


@router.put("/settings/model", dependencies=[Depends(require_uosa_scope("settings.write"))])
def update_model_settings(request: ModelSettingsUpdate):
    """Update the configured model identifier and/or temperature in the running API."""
    if request.model is None and request.temperature is None:
        raise HTTPException(
            status_code=400,
            detail="Provide a model name, temperature, or both.",
        )
    if request.model is not None and not _MODEL_NAME_PATTERN.fullmatch(request.model):
        raise HTTPException(status_code=422, detail="The model identifier is invalid.")

    config = _read_config()
    model_config = config.setdefault("model", {})
    if not isinstance(model_config, dict):
        raise HTTPException(status_code=503, detail="Model settings have an invalid format.")

    if request.model is not None:
        model_config["name"] = request.model
    if request.temperature is not None:
        model_config["temperature"] = float(request.temperature)

    _write_config(config)

    # Keep the imported model runtime in sync so changes apply without a restart.
    model_runtime.CONFIG = config
    model_runtime.MODEL = model_config["name"]
    model_runtime.TEMPERATURE = model_config.get("temperature", 0.7)
    model_runtime.URL = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{model_runtime.MODEL}:generateContent"
    )
    return {
        "status": "updated",
        "model": {
            "provider": model_config.get("provider", ""),
            "name": model_runtime.MODEL,
            "temperature": model_runtime.TEMPERATURE,
        },
    }


@router.put("/settings/memory", dependencies=[Depends(require_uosa_scope("settings.write"))])
def update_memory_settings(request: MemorySettingsUpdate):
    """Change current-conversation retention settings in config and runtime."""
    if request.enabled is None and request.max_messages is None:
        raise HTTPException(
            status_code=400,
            detail="Provide a memory enabled value, retention limit, or both.",
        )

    config = _read_config()
    memory_config = config.setdefault("memory", {})
    if not isinstance(memory_config, dict):
        raise HTTPException(status_code=503, detail="Memory settings have an invalid format.")

    if request.enabled is not None:
        memory_config["enabled"] = request.enabled
    if request.max_messages is not None:
        memory_config["max_messages"] = request.max_messages

    _write_config(config)

    context_runtime.CONFIG = config
    context_runtime.MEMORY_CONFIG = memory_config
    context_runtime.MEMORY_ENABLED = bool(memory_config.get("enabled", True))
    context_runtime.MAX_MESSAGES = int(memory_config.get("max_messages", 20))

    # If retention was reduced while memory is enabled, trim the on-disk session now.
    if request.max_messages is not None and context_runtime.MEMORY_ENABLED:
        context_runtime.trim_context()

    return {
        "status": "updated",
        "memory": {
            "enabled": context_runtime.MEMORY_ENABLED,
            "max_messages": context_runtime.MAX_MESSAGES,
        },
    }
