"""Resolve a raw line-item label to a canonical metric (registry-backed).

Matching strategy (safe, layered):
  1. Hard-normalize: lowercase + drop ALL non-alphanumerics (spaces, punctuation,
     mojibake). This alone absorbs space/whitespace OCR garble — e.g.
     "Cash and cash equivalen ts" -> "cashandcashequivalents" — so we do NOT
     enumerate garbled variants ourselves.
  2. Exact squashed match against the registry's aliases (handles known synonyms
     like turnover / net sales / revenue, since those are listed aliases).
  3. Fuzzy match (rapidfuzz ratio, high threshold) for truncation / character
     substitution OCR errors. Bounded by threshold to avoid false positives.
  4. Optional embedding fallback (off by default) for unseen synonyms.

Returns None when nothing clears the bar — the line simply stays untagged.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "data" / "canonical_metric_registry.json"
_NONALNUM = re.compile(r"[^a-z0-9]+")


def squash(text: str) -> str:
    """Lowercase, drop all non-alphanumerics (absorbs space/punct/mojibake garble)."""
    return _NONALNUM.sub("", text.lower())


def spaced(text: str) -> str:
    """Lowercase, non-alphanumerics -> single spaces (for fuzzy/token matching)."""
    return _NONALNUM.sub(" ", text.lower()).strip()


@dataclass(frozen=True)
class MetricMatch:
    canonical_key: str
    display_name: str
    category: str
    confidence: float
    method: str  # exact | fuzzy | embedding


class MetricResolver:
    def __init__(
        self,
        registry_path: str | Path | None = None,
        fuzzy_threshold: float | None = None,
        use_embeddings: bool | None = None,
    ) -> None:
        settings = get_settings()
        path = Path(registry_path or settings.metric_registry_path or _DEFAULT_REGISTRY)
        self._fuzzy_threshold = (
            fuzzy_threshold if fuzzy_threshold is not None else settings.metric_fuzzy_threshold
        )
        self._use_embeddings = (
            use_embeddings if use_embeddings is not None else settings.metric_use_embeddings
        )
        self._settings = settings

        registry: dict = json.loads(Path(path).read_text(encoding="utf-8"))
        self._display: dict[str, str] = {}
        self._category: dict[str, str] = {}
        self._exact: dict[str, str] = {}            # squashed alias -> canonical key
        self._choices: list[str] = []               # spaced aliases (fuzzy pool)
        self._choice_key: list[str] = []            # parallel canonical keys

        for key, meta in registry.items():
            display = meta.get("display_name", key)
            category = meta.get("category", "")
            self._display[key] = display
            self._category[key] = category
            surfaces = {key, display, *meta.get("aliases", [])}
            for surface in surfaces:
                sq = squash(surface)
                if sq and sq not in self._exact:
                    self._exact[sq] = key
                sp = spaced(surface)
                if sp:
                    self._choices.append(sp)
                    self._choice_key.append(key)

        self._embedder = None
        self._centroids = None
        logger.info("MetricResolver loaded %d metrics, %d aliases", len(self._display), len(self._choices))

    # --- embedding fallback (lazy, optional) ---

    def _ensure_embeddings(self) -> bool:
        if not self._use_embeddings:
            return False
        if self._embedder is not None:
            return self._centroids is not None
        from app.engines.extraction.services.embeddings import get_embedder

        self._embedder = get_embedder(self._settings.embedding_model)
        if self._embedder is None:
            return False
        import numpy as np

        self._np = np
        keys = list(self._display)
        texts = [self._display[k] for k in keys]
        vecs = np.asarray(self._embedder.encode(texts, normalize_embeddings=True, batch_size=64))
        self._centroids = (keys, vecs)
        return True

    def _embedding_match(self, label: str) -> MetricMatch | None:
        if not self._ensure_embeddings():
            return None
        np = self._np
        vec = np.asarray(self._embedder.encode([label], normalize_embeddings=True)[0])
        keys, vecs = self._centroids
        sims = vecs @ vec
        best = int(sims.argmax())
        score = float(sims[best])
        if score < 0.62:
            return None
        key = keys[best]
        return MetricMatch(key, self._display[key], self._category[key], score, "embedding")

    # --- main entry ---

    def resolve(self, label: str) -> MetricMatch | None:
        sq = squash(label)
        if not sq:
            return None

        key = self._exact.get(sq)
        if key is not None:
            return MetricMatch(key, self._display[key], self._category[key], 1.0, "exact")

        from rapidfuzz import fuzz, process

        match = process.extractOne(
            spaced(label),
            self._choices,
            scorer=fuzz.ratio,
            score_cutoff=self._fuzzy_threshold * 100,
        )
        if match is not None:
            _, score, idx = match
            key = self._choice_key[idx]
            return MetricMatch(key, self._display[key], self._category[key], score / 100.0, "fuzzy")

        return self._embedding_match(label)


@lru_cache(maxsize=1)
def get_resolver() -> MetricResolver:
    return MetricResolver()
