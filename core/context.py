import json
import os
import re
from threading import RLock

from core.conversations.store import get_conversation_store


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)


MEMORY_CONFIG = CONFIG.get("memory", {})

MEMORY_FILE = os.path.join(
    BASE_DIR,
    MEMORY_CONFIG.get("file", "memory/current_session.json")
)

MAX_MESSAGES = MEMORY_CONFIG.get("max_messages", 20)
MEMORY_ENABLED = MEMORY_CONFIG.get("enabled", True)

_CONTEXT_LOCK = RLock()


def _is_valid_context_message(item):
    """Validate the legacy role/text conversation-turn shape."""
    return (
        isinstance(item, dict)
        and isinstance(item.get("role"), str)
        and item["role"] in {"user", "assistant"}
        and isinstance(item.get("text"), str)
        and bool(item["text"].strip())
    )


def _load_context_records():
    """Read raw session records without filtering or mutating stored history.

    Return None for unreadable or structurally invalid stores so write paths can
    fail closed instead of replacing recoverable data with an empty session.
    """
    if not MEMORY_ENABLED:
        return []

    if not os.path.exists(MEMORY_FILE):
        return []

    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None

    return data if isinstance(data, list) else None


def load_context(conversation_id=None):
    """Load bounded context from one conversation or the legacy global store."""
    if conversation_id is not None:
        if not MEMORY_ENABLED:
            return []
        records = get_conversation_store().get_messages(
            conversation_id,
            limit=MAX_MESSAGES,
        )
        return [
            {"role": item["role"], "text": item["text"]}
            for item in records
            if _is_valid_context_message(item)
        ]

    records = _load_context_records()
    if not isinstance(records, list):
        return []

    return [item for item in records if _is_valid_context_message(item)]


STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "do", "does",
    "for", "from", "how", "i", "in", "is", "it", "me", "my",
    "of", "on", "or", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "why", "with",
    "you", "your"
}


def _tokenize(text):
    """Convert text into normalized word tokens."""
    if not isinstance(text, str):
        return set()

    return {
        token.lower()
        for token in re.findall(r"[a-zA-Z0-9]+", text)
        if token.lower() not in STOP_WORDS
    }


def _score_context_message(message, context_message, index):
    """Calculate deterministic relevance for one context message."""
    if not _is_valid_context_message(context_message):
        return 0

    message_tokens = _tokenize(message)
    text_tokens = _tokenize(context_message["text"])

    relevance = len(message_tokens & text_tokens)

    if relevance == 0:
        return 0

    # Recency only helps rank messages that are already relevant.
    recency = index + 1

    return relevance * 10 + recency


def _select_recent_context(history, limit):
    """Return the most recent valid context messages."""
    if not isinstance(history, list):
        return []

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []

    valid = [
        item
        for item in history
        if _is_valid_context_message(item)
    ]

    return valid[-limit:]


def select_relevant_context(
    message,
    history,
    limit=10,
    include_recent_fallback=False
):
    """
    Select the most relevant conversation messages.

    Selection is deterministic, read-only, and preserves chronological
    ordering in the returned result.
    """
    if not isinstance(message, str):
        return []

    if not isinstance(history, list):
        return []

    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []

    scored = []

    for index, item in enumerate(history):
        if not _is_valid_context_message(item):
            continue

        score = _score_context_message(
            message,
            item,
            index
        )

        if score > 0:
            scored.append((score, index, item))

    scored.sort(key=lambda item: (-item[0], -item[1]))

    selected = scored[:limit]

    # If relevant context is insufficient, fill remaining slots
    # with the most recent context messages.
    selected_indexes = {
        index
        for _, index, _ in selected
    }

    if include_recent_fallback and len(selected) < limit:
        for recent_index in range(len(history) - 1, -1, -1):
            item = history[recent_index]

            if not _is_valid_context_message(item):
                continue

            if recent_index in selected_indexes:
                continue

            selected.append((0, recent_index, item))
            selected_indexes.add(recent_index)

            if len(selected) >= limit:
                break

    # Restore original chronological order.
    selected.sort(key=lambda item: item[1])

    return [item for _, _, item in selected]


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


def save_context(messages):
    """Save the current conversation context atomically and serially."""
    if not MEMORY_ENABLED:
        return

    with _CONTEXT_LOCK:
        _atomic_json_write(
            MEMORY_FILE,
            messages[-MAX_MESSAGES:]
        )


def add_message(role, text, conversation_id=None):
    """Append a turn to one identified conversation or the legacy session."""
    if conversation_id is not None:
        # Transcript durability is independent of whether context is enabled
        # for model prompts. Disabling memory must not erase chat history.
        get_conversation_store().append_message(
            conversation_id,
            role,
            text,
        )
        return

    if not MEMORY_ENABLED:
        return

    with _CONTEXT_LOCK:
        messages = _load_context_records()
        if not isinstance(messages, list):
            return

        messages.append({
            "role": role,
            "text": text
        })

        save_context(messages)


def trim_context():
    """Apply the configured retention limit without discarding unknown records."""
    if not MEMORY_ENABLED:
        return

    with _CONTEXT_LOCK:
        messages = _load_context_records()
        if isinstance(messages, list):
            save_context(messages)


def clear_context():
    """Clear the current conversation serially with other context writes."""
    with _CONTEXT_LOCK:
        save_context([])
