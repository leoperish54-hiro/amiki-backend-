"""Verified self-knowledge contract for Amiki.

This module exposes only facts observable from Amiki's current configuration
and runtime state. It deliberately separates those facts from claims about
the underlying model implementation or training.
"""

import json
import os
from typing import Dict

from core.runtime_awareness import get_runtime_awareness


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

UNKNOWN_SELF_KNOWLEDGE = [
    "The private neural architecture of the underlying model is not exposed here.",
    "The underlying model's training data and training process are not exposed here.",
    "No absolute claim that Amiki is safe, unbiased, or incapable of hallucinating can be established from runtime state alone.",
    "A model-generated statement is not execution evidence unless a corresponding tool or runtime result exists.",
]


def build_self_knowledge_contract() -> Dict:
    """Build a safe, current self-knowledge snapshot for prompt grounding."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        config = json.load(file)

    assistant = config.get("assistant", {})
    identity = config.get("identity", {})
    model = config.get("model", {})
    memory = config.get("memory", {})
    database = config.get("database", {})
    modules = config.get("modules", {})

    runtime = get_runtime_awareness()

    return {
        "source": "amiki_runtime_and_configuration",
        "status": "verified",
        "identity": {
            "name": assistant.get("name", "Amiki"),
            "version": assistant.get("version", "unknown"),
            "role": identity.get(
                "role",
                "Personal AI assistant and digital companion",
            ),
        },
        "configured_model": {
            "provider": model.get("provider"),
            "name": model.get("name"),
            "temperature": model.get("temperature"),
        },
        "memory": {
            "enabled": bool(memory.get("enabled", False)),
            "current_session_file": memory.get("file"),
            "max_messages": memory.get("max_messages"),
            "persistent_memory_file": memory.get("long_term_file"),
        },
        "database": {
            "enabled": bool(database.get("enabled", False)),
            "path": database.get("path"),
        },
        "capabilities": {
            name: bool(enabled)
            for name, enabled in modules.items()
        },
        "runtime_observed": {
            "state": runtime.get("state"),
            "current_capability": runtime.get("current_capability"),
            "current_provider": runtime.get("current_provider"),
            "current_model": runtime.get("current_model"),
        },
        "unknown": list(UNKNOWN_SELF_KNOWLEDGE),
    }
