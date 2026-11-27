"""Conservative memory-cluster analysis.

This module identifies likely duplicates, related facts and contradictions.
It deliberately proposes rather than automatically deleting or rewriting facts.
"""
import re

from core.memory_embeddings import cosine_similarity


def _normalise(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _vector(fact):
    value = fact.get("_embedding")
    return value if isinstance(value, list) else None


def analyse_memory_clusters(memories, *, duplicate_threshold=0.96, related_threshold=0.74):
    if not isinstance(memories, list):
        return {"duplicates": [], "contradictions": [], "related": []}

    facts = [fact for fact in memories if isinstance(fact, dict)]
    duplicates = []
    contradictions = []
    related = []

    for index, left in enumerate(facts):
        for right in facts[index + 1:]:
            same_key = (
                left.get("category") == right.get("category")
                and left.get("key") == right.get("key")
            )
            same_value = _normalise(left.get("value", "")) == _normalise(right.get("value", ""))
            similarity = cosine_similarity(_vector(left), _vector(right))
            pair = {
                "left": left,
                "right": right,
                "similarity": round(similarity, 4),
            }

            left_key = set(_normalise(left.get("key", "")).split())
            right_key = set(_normalise(right.get("key", "")).split())
            key_overlap = (
                len(left_key & right_key) / max(len(left_key | right_key), 1)
            )
            same_category = left.get("category") == right.get("category")

            if same_key and not same_value:
                contradictions.append({**pair, "reason": "same_key_different_values"})
            elif same_value:
                duplicates.append({**pair, "reason": "equivalent_fact"})
            elif (
                same_category
                and similarity >= duplicate_threshold
                and key_overlap >= 0.25
            ):
                contradictions.append({
                    **pair,
                    "reason": "high_semantic_overlap_different_values",
                })
            elif similarity >= duplicate_threshold and similarity > 0:
                duplicates.append({**pair, "reason": "possible_semantic_duplicate"})
            elif similarity >= related_threshold:
                related.append({**pair, "reason": "semantic_similarity"})

    return {
        "duplicates": duplicates,
        "contradictions": contradictions,
        "related": related,
        "automatic_changes": 0,
    }


def review_memory_clusters():
    from core.memory import get_memory
    return analyse_memory_clusters(get_memory())
