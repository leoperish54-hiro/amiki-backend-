"""Optional local embedding provider for memory retrieval.

No model is downloaded or loaded unless memory.embeddings.enabled is true.
Install requirements-memory-embeddings.txt to enable the FastEmbed backend.
"""
import json
import os
from functools import lru_cache

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as _file:
    _CONFIG = json.load(_file)

_SETTINGS = _CONFIG.get("memory", {}).get("embeddings", {})


class EmbeddingUnavailable(RuntimeError):
    """Raised when semantic embeddings are enabled but cannot be loaded."""


@lru_cache(maxsize=1)
def _provider():
    if not _SETTINGS.get("enabled", False):
        return None
    provider = _SETTINGS.get("provider", "fastembed")
    if provider != "fastembed":
        raise EmbeddingUnavailable("Unsupported memory embedding provider.")
    try:
        from fastembed import TextEmbedding
        model_name = _SETTINGS.get("model", "BAAI/bge-small-en-v1.5")
        options = {}
        cache_dir = _SETTINGS.get("cache_dir")
        if cache_dir:
            options["cache_dir"] = os.path.join(BASE_DIR, cache_dir)
        return TextEmbedding(model_name=model_name, **options)
    except Exception as exc:
        raise EmbeddingUnavailable(
            "The configured local memory embedding model is unavailable."
        ) from exc


def embeddings_enabled():
    return bool(_SETTINGS.get("enabled", False))


def embed_text(text):
    """Return a normalized vector, or None when semantic search is disabled."""
    if not isinstance(text, str) or not text.strip() or not embeddings_enabled():
        return None
    model = _provider()
    if model is None:
        return None
    try:
        vector = next(iter(model.embed([text])))
        values = [float(value) for value in vector]
        norm = sum(value * value for value in values) ** 0.5
        if not norm:
            return None
        return [value / norm for value in values]
    except Exception as exc:
        raise EmbeddingUnavailable("Memory embedding generation failed.") from exc


def cosine_similarity(left, right):
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return 0.0
    if not left or len(left) != len(right):
        return 0.0
    try:
        a = [float(value) for value in left]
        b = [float(value) for value in right]
    except (TypeError, ValueError):
        return 0.0
    a_norm = sum(value * value for value in a) ** 0.5
    b_norm = sum(value * value for value in b) ** 0.5
    if not a_norm or not b_norm:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (a_norm * b_norm)
