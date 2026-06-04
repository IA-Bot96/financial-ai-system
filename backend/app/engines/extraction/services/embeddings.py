"""Shared local sentence-embedding model (free, no API). Loaded once, lazily."""
from __future__ import annotations

from functools import lru_cache

from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache(maxsize=2)
def get_embedder(model_name: str):
    """Return a cached SentenceTransformer, or None if unavailable."""
    try:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model %s", model_name)
        return SentenceTransformer(model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding model unavailable (%s).", exc)
        return None
