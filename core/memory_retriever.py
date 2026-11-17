import re


DEFAULT_THRESHOLD = 2
DEFAULT_LIMIT = 5

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
        for token in re.findall(r"[a-zA-Z0-9]+", text.replace("_", " "))
        if token.lower() not in STOP_WORDS
    }


def _score_memory(message, memory):
    """Calculate deterministic relevance score for one memory."""
    if not isinstance(memory, dict):
        return 0

    message_tokens = _tokenize(message)

    key = memory.get("key", "")
    value = memory.get("value", "")
    category = memory.get("category", "")

    key_tokens = _tokenize(key)
    value_tokens = _tokenize(value)
    category_tokens = _tokenize(category)

    score = 0

    if key_tokens and key_tokens.issubset(message_tokens):
        score += 5
    elif key_tokens & message_tokens:
        score += 3

    if value_tokens & message_tokens:
        score += 2

    if category_tokens & message_tokens:
        score += 1

    return score

def retrieve_memories(message, memories, threshold=DEFAULT_THRESHOLD, limit=DEFAULT_LIMIT):
    """
    Retrieve memories relevant to a user message.

    Exact key matches take priority over weaker partial-key matches.
    Results are ranked by descending relevance score.
    Ties preserve original memory order.

    This function is read-only and never modifies the supplied memories.
    """
    if not isinstance(message, str):
        return []

    if not isinstance(memories, list):
        return []

    scored = []

    for index, memory in enumerate(memories):
        score = _score_memory(message, memory)

        if score >= threshold:
            scored.append((score, index, memory))

    if not scored:
        return []

    exact_key_matches = [
        item
        for item in scored
        if _tokenize(item[2].get("key", ""))
        and _tokenize(item[2].get("key", "")).issubset(_tokenize(message))
    ]

    if exact_key_matches:
        scored = exact_key_matches

    scored.sort(key=lambda item: (-item[0], item[1]))

    return [
        memory
        for _, _, memory in scored[:limit]
    ]
