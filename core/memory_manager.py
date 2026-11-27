from core import memory as memory_store
from core.memory import add_memory, get_memory, delete_memory


PERSISTENT_CATEGORIES = {
    "preference",
    "personal_fact",
    "project_fact",
}


def _non_empty_text(value):
    return isinstance(value, str) and bool(value.strip())


def is_persistent_category(category):
    """Return True when a category is suitable for long-term storage."""
    return isinstance(category, str) and category in PERSISTENT_CATEGORIES


def remember(
    category,
    key,
    value,
    *,
    provenance=None,
    confidence=0.9,
    source_text=None,
    source_type="explicit_user_statement",
    extraction_method="pattern",
    correlation_id=None,
):
    """
    Store or update a validated long-term fact.

    The storage layer performs the mutation under its lock. A read-back
    confirms that disabled or unsuccessful persistence is never reported
    as a successful remember operation.
    """
    if not memory_store.MEMORY_ENABLED:
        return False
    if not is_persistent_category(category):
        return False
    if not _non_empty_text(key) or not _non_empty_text(value):
        return False

    add_memory(
        category,
        key,
        value,
        provenance=provenance,
        confidence=confidence,
        source_text=source_text,
        source_type=source_type,
        extraction_method=extraction_method,
        correlation_id=correlation_id,
    )
    persisted = get_memory(category, key)
    return any(fact.get("value") == value for fact in persisted)


def forget(category, key):
    """
    Remove a long-term fact.

    Returns False for invalid category/key input or when persistent memory
    is disabled. A valid request returns True even when no matching fact
    existed; storage failures propagate.
    """
    if not memory_store.MEMORY_ENABLED:
        return False
    if not is_persistent_category(category) or not _non_empty_text(key):
        return False

    delete_memory(category, key)
    return True


def recall(category=None, key=None):
    """Retrieve validated long-term memories."""
    if category is not None and not _non_empty_text(category):
        return []
    if key is not None and not _non_empty_text(key):
        return []
    return get_memory(category, key)


def should_remember(category):
    """Determine whether a category belongs in long-term memory."""
    return is_persistent_category(category)
