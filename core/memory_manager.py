from core.memory import add_memory, get_memory, delete_memory


PERSISTENT_CATEGORIES = {
    "preference",
    "personal_fact",
    "project_fact"
}


def is_persistent_category(category):
    """Return True when a category is suitable for long-term storage."""
    return category in PERSISTENT_CATEGORIES


def remember(category, key, value):
    """
    Store a long-term memory.

    If the same category and key already exist, update the
    existing memory through the memory layer.
    """
    if not is_persistent_category(category):
        return False

    if not key or not value:
        return False

    existing = get_memory(category, key)

    if existing:
        existing_value = existing[0].get("value")

        if existing_value == value:
            return True

    add_memory(category, key, value)

    # The storage layer may intentionally do nothing (for example when
    # persistent memory is disabled). Report success only after read-back.
    persisted = get_memory(category, key)
    return any(
        fact.get("value") == value
        for fact in persisted
    )


def forget(category, key):
    """
    Remove a long-term memory.

    Returns False when the category is not persistent or the key
    is invalid. Returns True when the operation is valid, even if
    the memory did not already exist.
    """
    if not is_persistent_category(category):
        return False

    if not key:
        return False

    delete_memory(category, key)
    return True


def recall(category=None, key=None):
    """Retrieve long-term memories."""
    return get_memory(category, key)


def should_remember(category):
    """Determine whether a category belongs in long-term memory."""
    return is_persistent_category(category)
