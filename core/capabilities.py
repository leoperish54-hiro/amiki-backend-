"""Capability registry for Amiki."""

import json
import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)


MODULES = CONFIG.get("modules", {})

CAPABILITIES = {
    "analysis": bool(MODULES.get("analysis", False)),
    "university": bool(MODULES.get("university", False)),
    "workspace": bool(MODULES.get("workspace", False)),
    "noctis_requiem": bool(MODULES.get("noctis_requiem", False)),
    "web": bool(MODULES.get("web", False)),
    "device": bool(MODULES.get("device", False)),
}


def get_capabilities():
    """Return the currently available capabilities."""
    return CAPABILITIES.copy()


def is_available(name):
    """Return whether a capability is currently available."""
    return CAPABILITIES.get(name, False)
