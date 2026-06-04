"""De-duplicate insights: exact key first, then semantic (embedding cosine).

Semantic dedup removes the near-duplicate bloat that overlapping sliding-window
chunks produce (the old pipeline used exact-match only and leaked paraphrases).
Falls back to exact-only if the embedding model is unavailable.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.insight import Insight
from app.engines.extraction.services.embeddings import get_embedder

logger = get_logger(__name__)


def _exact_dedup(insights: list[Insight]) -> list[Insight]:
    seen: set[tuple] = set()
    out: list[Insight] = []
    for ins in insights:
        key = (
            ins.area.strip().lower(),
            ins.takeaway.strip().lower(),
            (ins.source_section or "").strip().lower(),
            ins.year,
            ins.source_report_year,
            ins.page,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(ins)
    return out


def dedup_insights(insights: list[Insight]) -> list[Insight]:
    insights = _exact_dedup(insights)
    if len(insights) < 2:
        return insights

    settings = get_settings()
    embedder = get_embedder(settings.embedding_model)
    if embedder is None:
        return insights  # exact-only fallback

    import numpy as np

    # Highest-confidence first so duplicates collapse onto the best phrasing.
    order = sorted(range(len(insights)), key=lambda i: insights[i].confidence, reverse=True)
    texts = [insights[i].takeaway for i in order]
    vecs = np.asarray(embedder.encode(texts, normalize_embeddings=True, batch_size=64))

    threshold = settings.insight_dedup_similarity
    kept_idx: list[int] = []
    kept_vecs: list = []
    for pos, original_i in enumerate(order):
        v = vecs[pos]
        if any(float(np.dot(v, kv)) >= threshold for kv in kept_vecs):
            continue
        kept_idx.append(original_i)
        kept_vecs.append(v)

    deduped = [insights[i] for i in sorted(kept_idx)]
    logger.info("Semantic dedup: %d -> %d insights", len(insights), len(deduped))
    return deduped
