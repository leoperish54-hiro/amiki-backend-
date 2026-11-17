import json
import os
from threading import RLock


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)


MEMORY_CONFIG = CONFIG.get("memory", {})

LONG_TERM_FILE = os.path.join(
    BASE_DIR,
    MEMORY_CONFIG.get(
        "long_term_file",
        "memory/persistent_memory.json"
    )
)

MEMORY_ENABLED = MEMORY_CONFIG.get("enabled", True)

_MEMORY_LOCK = RLock()


def load_memory():
    """Load persistent long-term memory."""
    if not MEMORY_ENABLED:
        return {"facts": []}

    if not os.path.exists(LONG_TERM_FILE):
        return {"facts": []}

    try:
        with open(LONG_TERM_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            data.setdefault("facts", [])
            return data

    except (json.JSONDecodeError, OSError):
        pass

    return {"facts": []}


def _atomic_json_write(file_path, data):
    """Write JSON atomically so interrupted writes preserve the old file."""
    target = os.path.abspath(file_path)
    directory = os.path.dirname(target)

    if directory:
        os.makedirs(directory, exist_ok=True)

    temporary = f"{target}.tmp"

    try:
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def save_memory(memory):
    """Save persistent long-term memory atomically and serially."""
    if not MEMORY_ENABLED:
        return

    with _MEMORY_LOCK:
        _atomic_json_write(
            LONG_TERM_FILE,
            memory
        )


def add_memory(category, key, value):
    """Add or update a persistent memory fact without lost concurrent writes."""
    if not MEMORY_ENABLED:
        return

    with _MEMORY_LOCK:
        memory = load_memory()
        facts = memory.setdefault("facts", [])

        for fact in facts:
            if (
                fact.get("category") == category
                and fact.get("key") == key
            ):
                fact["value"] = value
                save_memory(memory)
                return

        facts.append({
            "category": category,
            "key": key,
            "value": value
        })

        save_memory(memory)


def get_memory(category=None, key=None):
    """Retrieve persistent memory facts."""
    memory = load_memory()
    facts = memory.get("facts", [])

    if category is None and key is None:
        return facts

    results = []

    for fact in facts:
        if category is not None and fact.get("category") != category:
            continue

        if key is not None and fact.get("key") != key:
            continue

        results.append(fact)

    return results


def delete_memory(category, key):
    """Delete a persistent memory fact without racing concurrent writes."""
    if not MEMORY_ENABLED:
        return

    with _MEMORY_LOCK:
        memory = load_memory()

        memory["facts"] = [
            fact
            for fact in memory.get("facts", [])
            if not (
                fact.get("category") == category
                and fact.get("key") == key
            )
        ]

        save_memory(memory)


def clear_memory():
    """Clear all persistent long-term memory serially with other writes."""
    if not MEMORY_ENABLED:
        return

    with _MEMORY_LOCK:
        save_memory({"facts": []})
