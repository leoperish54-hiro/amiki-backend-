"""Persistent academic state for Amiki's university workspace."""
import json
import os
from threading import RLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
ACADEMIC_FILE = os.path.join(BASE_DIR, "database", "academic.json")
_ACADEMIC_LOCK = RLock()

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

ACADEMIC_CONFIG = CONFIG.get("academic", {})


def _defaults():
    return {
        "institution": ACADEMIC_CONFIG.get("institution", ""),
        "program": ACADEMIC_CONFIG.get("program", ""),
        "duration_years": ACADEMIC_CONFIG.get("duration_years", 0),
        "current_year": ACADEMIC_CONFIG.get("current_year", 0),
        "current_semester": ACADEMIC_CONFIG.get("current_semester", 0),
        "current_session": ACADEMIC_CONFIG.get("current_session", ""),
        "courses": list(ACADEMIC_CONFIG.get("courses", [])),
        "progress_policy": ACADEMIC_CONFIG.get("progress_policy", ""),
    }


def load_academic_state():
    with _ACADEMIC_LOCK:
        if not os.path.exists(ACADEMIC_FILE):
            return _defaults()
        try:
            with open(ACADEMIC_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError, UnicodeError):
            return _defaults()
        if not isinstance(data, dict):
            return _defaults()
        state = _defaults()
        state.update(data)
        if not isinstance(state.get("courses"), list):
            state["courses"] = []
        return state


def save_academic_state(state):
    if not isinstance(state, dict):
        raise ValueError("Academic state must be a dictionary.")
    with _ACADEMIC_LOCK:
        target = os.path.abspath(ACADEMIC_FILE)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = f"{target}.tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(state, file, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


def get_academic_state():
    return load_academic_state()
