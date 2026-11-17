"""Conservative screening for explicit-negation conflicts between approved claims.

This is a lexical review aid, not semantic reasoning or truth adjudication.
It never changes claim status or treats a flagged pair as a proven contradiction.
"""
import re
from collections.abc import Mapping


_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_NEGATIONS = {"not", "no", "never", "without", "neither", "cannot"}
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "at", "in", "on", "of", "to", "for", "and",
    "or", "as", "by", "with", "that", "this", "it", "its",
}


def _claim_terms(value):
    tokens = [item.casefold() for item in _TOKEN.findall(value)]
    negated = any(item in _NEGATIONS for item in tokens)
    terms = {
        item for item in tokens
        if item not in _NEGATIONS and item not in _STOPWORDS and len(item) > 1
    }
    return terms, negated


def find_potential_conflicts(claims, min_shared_terms=3, min_similarity=0.8):
    """Return review-only pairs differing by explicit negation over near-identical terms.

    Only records with status='approved' are considered. Similarity is Jaccard
    similarity over content terms after removing explicit negation markers.
    """
    if isinstance(min_shared_terms, bool) or not isinstance(min_shared_terms, int) or min_shared_terms < 1:
        raise ValueError("min_shared_terms must be a positive integer")
    if (
        isinstance(min_similarity, bool)
        or not isinstance(min_similarity, (int, float))
        or not 0 < min_similarity <= 1
    ):
        raise ValueError("min_similarity must be between 0 and 1")
    if not isinstance(claims, (list, tuple)):
        raise ValueError("claims must be a list or tuple")

    prepared = []
    for item in claims:
        if not isinstance(item, Mapping) or item.get("status") != "approved":
            continue
        claim_id = item.get("id")
        statement = item.get("claim")
        if not isinstance(claim_id, str) or not claim_id or not isinstance(statement, str) or not statement.strip():
            continue
        terms, negated = _claim_terms(statement)
        if len(terms) >= min_shared_terms:
            prepared.append((claim_id, statement.strip(), terms, negated))

    conflicts = []
    for index, left in enumerate(prepared):
        for right in prepared[index + 1:]:
            if left[3] == right[3]:
                continue
            shared = left[2] & right[2]
            union = left[2] | right[2]
            similarity = len(shared) / len(union) if union else 0.0
            if len(shared) < min_shared_terms or similarity < min_similarity:
                continue
            conflicts.append({
                "claim_ids": [left[0], right[0]],
                "claims": [left[1], right[1]],
                "relation": "possible_explicit_negation_conflict",
                "shared_terms": sorted(shared),
                "similarity": round(similarity, 4),
                "method": "negation-and-token-overlap-heuristic",
                "review_required": True,
                "disclaimer": (
                    "Potential conflict only; lexical screening does not establish "
                    "semantic contradiction or determine which claim is correct."
                ),
            })
    return conflicts


__all__ = ["find_potential_conflicts"]
