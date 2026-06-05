"""Free, local table classifier — Stage 1 of Layer 2.

Combines:
  - rapidfuzz keyword matching (exact substring -> full score, fuzzy -> capped),
  - sentence-transformers embedding similarity vs per-type prototype centroids.

No API cost. If sentence-transformers is unavailable or disabled, it degrades
gracefully to keyword-only scoring. Tables that don't clear the confidence /
margin gate are returned as `unclassified` with `needs_review=True` so GPT
(Stage 2) can resolve them.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.services.keywords import (
    EMBEDDING_PROTOTYPES,
    STATEMENT_KEYWORDS,
)

logger = get_logger(__name__)

_FUZZY_CAP = 0.85  # fuzzy (non-exact) matches can't score as high as exact substrings


@dataclass
class Classification:
    statement_type: StatementType
    score: float
    method: str
    needs_review: bool
    scores: dict[StatementType, float]


class TableClassifier:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._embedder = None
        self._centroids: dict[StatementType, "object"] | None = None
        self._embed_failed = False

    # --- embeddings (lazy, optional) ---

    def _ensure_embedder(self) -> bool:
        if not self.settings.use_embeddings or self._embed_failed:
            return False
        if self._embedder is not None:
            return True
        try:
            import numpy as np

            from app.engines.extraction.services.embeddings import get_embedder

            embedder = get_embedder(self.settings.embedding_model)
            if embedder is None:
                self._embed_failed = True
                return False
            self._np = np
            self._embedder = embedder
            self._centroids = self._build_centroids()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embeddings unavailable (%s); using keyword-only scoring.", exc)
            self._embed_failed = True
            return False

    def _build_centroids(self) -> dict[StatementType, object]:
        np = self._np
        centroids: dict[StatementType, object] = {}
        for st, prototypes in EMBEDDING_PROTOTYPES.items():
            vecs = self._embedder.encode(
                prototypes, normalize_embeddings=True, batch_size=32
            )
            centroid = np.asarray(vecs).mean(axis=0)
            norm = np.linalg.norm(centroid)
            centroids[st] = centroid / norm if norm else centroid
        return centroids

    def _embed_scores(self, text: str) -> dict[StatementType, float]:
        if not self._ensure_embedder():
            return {}
        np = self._np
        vec = np.asarray(self._embedder.encode([text], normalize_embeddings=True)[0])
        scores: dict[StatementType, float] = {}
        for st, centroid in self._centroids.items():
            scores[st] = max(0.0, float(np.dot(vec, centroid)))
        return scores

    # --- fuzzy keyword scoring ---

    @staticmethod
    def _fuzzy_scores(text: str) -> dict[StatementType, float]:
        from rapidfuzz import fuzz

        low = text.lower()
        scores: dict[StatementType, float] = {}
        for st, keywords in STATEMENT_KEYWORDS.items():
            best = 0.0
            for kw in keywords:
                if kw in low:
                    best = 1.0
                    break
                best = max(best, (fuzz.partial_ratio(kw, low) / 100.0) * _FUZZY_CAP)
            scores[st] = best
        return scores

    # --- main entry ---

    def classify(self, signature_text: str, section_hint: StatementType | None = None) -> Classification:
        fuzzy = self._fuzzy_scores(signature_text)
        embed = self._embed_scores(signature_text)

        fw = self.settings.classify_fuzzy_weight
        ew = self.settings.classify_embed_weight if embed else 0.0
        denom = fw + ew or 1.0

        combined: dict[StatementType, float] = {}
        for st in STATEMENT_KEYWORDS:
            combined[st] = (fw * fuzzy.get(st, 0.0) + ew * embed.get(st, 0.0)) / denom

        if section_hint and section_hint != StatementType.other:
            combined[section_hint] = min(
                1.0, combined.get(section_hint, 0.0) + self.settings.classify_section_hint_boost
            )

        ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
        best_type, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best_score - runner_up

        method = "fuzzy+embed" if embed else "fuzzy"
        if best_score >= self.settings.classify_accept_threshold and margin >= self.settings.classify_margin:
            return Classification(best_type, best_score, method, False, combined)
        # Not confident -> mark unclassified so the interpretation stage sends it to GPT.
        return Classification(StatementType.unclassified, best_score, method + "+ambiguous", True, combined)
