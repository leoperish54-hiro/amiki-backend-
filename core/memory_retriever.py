import re


DEFAULT_THRESHOLD = 2
DEFAULT_LIMIT = 5

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but",
    "by", "do", "does", "did", "for", "from", "had", "has", "have",
    "how", "i", "in", "is", "it", "me", "my", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "what", "when",
    "where", "which", "who", "why", "with", "you", "your",
}

# Small, explicit concept aliases improve paraphrase recall without requiring
# a network model, embeddings service, or unbounded retrieval.
MEMORY_QUERY_ALIASES = {
    "name": ("my name", "preferred name", "what to call me"),
    "favorite_anime": (
        "favorite anime", "favourite anime", "anime i like",
        "anime i enjoy", "anime i enjoyed", "anime i love",
        "anime i prefer", "series i enjoy most",
    ),
    "favorite_vehicle": (
        "favorite car", "favourite car", "favorite vehicle",
        "favourite vehicle", "vehicle i like", "car i like",
        "car i enjoy", "dream car", "preferred vehicle",
    ),
    "favorite_car": (
        "favorite car", "favourite car", "favorite vehicle",
        "favourite vehicle", "vehicle i like", "car i like",
        "car i enjoy", "dream car",
    ),
    "field_of_study": (
        "field of study", "degree", "major", "program of study",
        "course of study", "what i study", "what degree i pursue",
    ),
    "education_plan": (
        "education plan", "what i plan to study", "where i plan to study",
        "university plan", "course i plan to take",
    ),
    "response_style": (
        "response style", "answer style", "how i prefer answers",
        "how i prefer explanations", "preferred explanation style",
        "concise explanations", "short answers", "brief explanations",
    ),
    "dislikes_large_crowds": (
        "large crowds", "huge crowds", "being in crowds",
        "crowded places", "crowd preference",
    ),
    "interests": (
        "things i am into", "my interests", "what i am interested in",
        "hobbies and interests",
    ),
}


def _tokenize(text):
    """Normalize text into case-insensitive alphanumeric tokens."""
    if not isinstance(text, str):
        return set()

    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold().replace("_", " "))
        if token not in STOP_WORDS
    }


def _alias_score(message_tokens, key):
    aliases = MEMORY_QUERY_ALIASES.get(key, ())
    best = 0
    for alias in aliases:
        alias_tokens = _tokenize(alias)
        if alias_tokens and alias_tokens.issubset(message_tokens):
            best = max(best, 8 + len(alias_tokens))
    return best


def _score_memory(message_tokens, memory):
    """Score a fact using exact keys, curated concepts, and lexical evidence."""
    if not isinstance(memory, dict):
        return 0

    key = memory.get("key", "")
    value = memory.get("value", "")
    category = memory.get("category", "")

    key_tokens = _tokenize(key)
    value_tokens = _tokenize(value)
    category_tokens = _tokenize(category)

    score = 0
    if key_tokens and key_tokens.issubset(message_tokens):
        score += 10
    elif key_tokens & message_tokens:
        score += 3

    score = max(score, _alias_score(message_tokens, key))

    if value_tokens:
        overlap = len(value_tokens & message_tokens)
        if overlap:
            score += min(overlap * 2, 4)

    if category_tokens & message_tokens:
        score += 1

    return score


def retrieve_memories(
    message,
    memories,
    threshold=DEFAULT_THRESHOLD,
    limit=DEFAULT_LIMIT,
):
    """
    Retrieve a bounded, relevance-ranked set of long-term facts.

    Exact key matches and curated natural-language aliases outrank incidental
    token overlap. Ties preserve source order; the input is never mutated.
    """
    if not isinstance(message, str) or not message.strip():
        return []
    if not isinstance(memories, list):
        return []
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return []
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return []

    message_tokens = _tokenize(message)
    if not message_tokens:
        return []

    scored = []
    exact_matches = []

    for index, fact in enumerate(memories):
        if not isinstance(fact, dict):
            continue
        key_tokens = _tokenize(fact.get("key", ""))
        score = _score_memory(message_tokens, fact)
        if key_tokens and key_tokens.issubset(message_tokens):
            exact_matches.append((score, index, fact))
        if score >= threshold:
            scored.append((score, index, fact))

    # Exact key matches suppress incidental partial-token matches. Also
    # retain curated aliases for other concepts in a compound question.
    if exact_matches:
        selected = {index: item for item in exact_matches if item[0] >= threshold}
        for item in scored:
            _score, index, fact = item
            if _alias_score(message_tokens, fact.get("key", "")):
                selected[index] = item
        scored = list(selected.values())

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [fact for _, _, fact in scored[:limit]]
