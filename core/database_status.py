"""Build safe status information for Amiki's data stores."""

import json
import os

from core.context import (
    MEMORY_ENABLED as SESSION_MEMORY_ENABLED,
    MEMORY_FILE,
    MAX_MESSAGES,
    load_context,
)
from core.memory import (
    LONG_TERM_FILE,
    MEMORY_ENABLED as PERSISTENT_MEMORY_ENABLED,
)
from core.memory_manager import recall


def _check_json_store(file_path, expected_type):
    """Check whether a JSON store exists and has the expected structure."""
    if not os.path.exists(file_path):
        return "degraded"

    if not os.path.isfile(file_path):
        return "degraded"

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return "degraded"

    if not isinstance(data, expected_type):
        return "degraded"

    return "healthy"


def build_database_status():
    """Return safe, user-facing status information for Amiki's data stores."""
    persistent_memories = recall()
    session_context = load_context()

    persistent_categories = {
        item.get("category")
        for item in persistent_memories
        if isinstance(item, dict) and item.get("category")
    }

    persistent_store_status = (
        _check_json_store(LONG_TERM_FILE, dict)
        if PERSISTENT_MEMORY_ENABLED
        else "disabled"
    )

    session_store_status = (
        _check_json_store(MEMORY_FILE, list)
        if SESSION_MEMORY_ENABLED
        else "disabled"
    )

    implemented_store_statuses = [
        persistent_store_status,
        session_store_status,
    ]

    if any(
        status == "degraded"
        for status in implemented_store_statuses
    ):
        overall_status = "degraded"
    elif all(
        status == "disabled"
        for status in implemented_store_statuses
    ):
        overall_status = "disabled"
    else:
        overall_status = "healthy"

    return {
        "overall_status": overall_status,
        "persistent_memory": {
            "enabled": PERSISTENT_MEMORY_ENABLED,
            "count": len(persistent_memories),
            "categories": len(persistent_categories),
            "status": persistent_store_status,
        },
        "current_session": {
            "enabled": SESSION_MEMORY_ENABLED,
            "active": bool(session_context),
            "message_count": len(session_context),
            "max_messages": MAX_MESSAGES,
            "status": session_store_status,
        },
        "attachments": {
            "available": True,
            "persistent": False,
            "status": "pipeline_only",
        },
        "chat_logs": {
            "available": False,
            "persistent": False,
            "status": "not_configured",
        },
    }
