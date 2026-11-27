import json
import os
import tempfile
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


class MemoryStoreError(RuntimeError):
    """Raised when persistent memory cannot be safely read or validated."""


def _validate_memory(memory):
    """Validate the persisted document without discarding forward-compatible fields."""
    if not isinstance(memory, dict):
        raise MemoryStoreError("Persistent memory has an invalid document structure.")

    facts = memory.get("facts")
    if not isinstance(facts, list):
        raise MemoryStoreError("Persistent memory facts must be stored as a list.")

    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise MemoryStoreError(
                f"Persistent memory contains an invalid fact at position {index}."
            )
        for field in ("category", "key", "value"):
            value = fact.get(field)
            if not isinstance(value, str) or not value.strip():
                raise MemoryStoreError(
                    f"Persistent memory contains an invalid {field} at position {index}."
                )

    return memory


def _validate_fact_fields(category, key, value):
    for field, item in (("category", category), ("key", key), ("value", value)):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Memory {field} must be a non-empty string.")


def load_memory():
    """Load persistent long-term memory; never disguise corruption as an empty store."""
    if not MEMORY_ENABLED:
        return {"facts": []}

    with _MEMORY_LOCK:
        try:
            with open(LONG_TERM_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            return {"facts": []}
        except json.JSONDecodeError:
            raise MemoryStoreError(
                "Persistent memory contains invalid JSON and was not changed."
            ) from None
        except (OSError, UnicodeError):
            raise MemoryStoreError(
                "Persistent memory could not be read and was not changed."
            ) from None

        return _validate_memory(data)


def _atomic_json_write(file_path, data):
    """Durably publish a complete JSON document without exposing partial writes."""
    target = os.path.abspath(file_path)
    directory = os.path.dirname(target) or os.curdir
    os.makedirs(directory, exist_ok=True)

    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=os.path.basename(target) + ".",
            suffix=".tmp",
            dir=directory,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            descriptor = None
            json.dump(data, file, indent=2, ensure_ascii=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary, target)
        temporary = None

        # Persist the directory entry where supported (POSIX/Android).
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def save_memory(memory):
    """Validate and atomically save persistent long-term memory."""
    if not MEMORY_ENABLED:
        return

    _validate_memory(memory)
    with _MEMORY_LOCK:
        _atomic_json_write(LONG_TERM_FILE, memory)


def add_memory(category, key, value):
    """Add or update one fact without losing concurrent in-process writes."""
    if not MEMORY_ENABLED:
        return

    _validate_fact_fields(category, key, value)
    with _MEMORY_LOCK:
        memory = load_memory()
        for fact in memory["facts"]:
            if fact["category"] == category and fact["key"] == key:
                fact["value"] = value
                save_memory(memory)
                return

        memory["facts"].append({
            "category": category,
            "key": key,
            "value": value,
        })
        save_memory(memory)


def get_memory(category=None, key=None):
    """Retrieve validated persistent memory facts."""
    with _MEMORY_LOCK:
        facts = load_memory()["facts"]

        if category is None and key is None:
            return facts

        results = []
        for fact in facts:
            if category is not None and fact["category"] != category:
                continue
            if key is not None and fact["key"] != key:
                continue
            results.append(fact)
        return results


def delete_memory(category, key):
    """Delete matching facts while preserving all other store metadata."""
    if not MEMORY_ENABLED:
        return

    _validate_fact_fields(category, key, "delete-operation")
    with _MEMORY_LOCK:
        data = load_memory()
        data["facts"] = [
            fact for fact in data["facts"]
            if not (fact["category"] == category and fact["key"] == key)
        ]
        save_memory(data)


def clear_memory():
    """Explicitly clear all persistent memory, including an unreadable store."""
    if not MEMORY_ENABLED:
        return

    # This is an intentional owner-requested destructive action, not recovery
    # performed as a side effect of a failed read or ordinary memory mutation.
    with _MEMORY_LOCK:
        save_memory({"facts": []})
