"""Conservative, deterministic extraction helpers for ALE knowledge synthesis.

These helpers identify candidate statements from already-approved source records.
They do not judge truth, resolve contradictions, or promote extracted content.
"""
import re

from core.web_evidence import contains_instruction_pattern


_MAX_CLAIM_LENGTH = 600
_MIN_CLAIM_LENGTH = 30
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def normalize_claim(value):
    """Normalize a claim for exact-text deduplication, not semantic equivalence."""
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).split())


def extract_claims(content, max_claims=40):
    """Extract bounded sentence-sized review candidates from approved source text."""
    if not isinstance(content, str) or not content.strip():
        return []
    if isinstance(max_claims, bool) or not isinstance(max_claims, int) or not 1 <= max_claims <= 100:
        raise ValueError("max_claims must be between 1 and 100")

    text = content.strip()
    if "Reference only:" in text and "Extracted page content (untrusted):" not in text:
        return []
    marker = "Extracted page content (untrusted):"
    if marker in text:
        text = text.split(marker, 1)[1].strip()

    text = " ".join(text.split())
    if not text:
        return []

    candidates = []
    seen = set()
    for raw in _SENTENCE_SPLIT.split(text):
        claim = " ".join(raw.strip(" \t\r\n•-*").split())
        if not (_MIN_CLAIM_LENGTH <= len(claim) <= _MAX_CLAIM_LENGTH):
            continue
        if contains_instruction_pattern(claim):
            continue
        normalized = normalize_claim(claim)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(claim)
        if len(candidates) >= max_claims:
            break
    return candidates


__all__ = ["extract_claims", "normalize_claim"]
