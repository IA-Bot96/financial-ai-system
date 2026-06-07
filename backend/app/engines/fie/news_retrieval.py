"""News semantic retrieval (L6 refinement) — chunk → embed → rank → dedup.

Turns the raw per-article news EvidenceItems from the failover adapter into the
small, query-relevant, de-duplicated set that the LLM actually narrates over:

  1. CHUNK    each article (sliding window over long bodies; snippet/title as a
              single chunk for short items — most free-tier news is snippet-length).
  2. EMBED    each chunk with the shared local model (bge-small; free, offline).
  3. RANK     by cosine to the query, blended with recency; keep top-K above a floor.
  4. DEDUP    near-identical chunks (wire-service syndication) by cosine, keep-best.

Each surviving chunk keeps its article citation (source / author / link), so every
chunk fed to the LLM is traceable in the final response. Degrades gracefully: with
no embedding model it falls back to recency-ordered, title-deduped articles.

Provenance is never mutated — only the chunk text + relevance score are added to the
citation locator; the source/author/link set by the news adapter is preserved.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Callable, Optional

from .models import EvidenceItem

_log = logging.getLogger("app.engines.fie")


def build_query_text(frame, company: Optional[str] = None) -> str:
    """Embed target: company + the raw query + any required metrics (the 'query
    object', richer than the bare string)."""
    parts = [company or getattr(frame, "company", None), getattr(frame, "raw_query", None)]
    parts += list(getattr(frame, "metrics", None) or [])
    return " ".join(str(p) for p in parts if p).strip()


def _chunk(text: str, size: int, overlap: int) -> list[str]:
    """Sliding window over characters (no tokenizer dependency)."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        if i + size >= len(text):
            break
        i += step
    return out


def _recency(published_at: Optional[str], anchor: Optional[str], halflife_days: int) -> float:
    """0..1; newer → higher. Neutral 0.5 when the date can't be parsed."""
    if not published_at:
        return 0.5
    try:
        pub = date.fromisoformat(str(published_at)[:10])
        end = date.fromisoformat(anchor[:10]) if anchor else date.today()
    except ValueError:
        return 0.5
    age = max(0, (end - pub).days)
    return 0.5 ** (age / max(1, halflife_days))


def _article_chunks(ev: EvidenceItem, settings) -> list[tuple[str, str]]:
    """(score_text, chunk_text) per chunk of one article. score_text prepends the
    title for signal; chunk_text is the content piece stored on the chunk."""
    loc = ev.citations[0].locator if ev.citations else {}
    title = ev.claim or ""
    body = (loc.get("content") or "").strip()
    snippet = (loc.get("snippet") or "").strip()
    base = body if len(body) >= settings.news_min_body_chars else (snippet or body)
    if base and len(base) >= settings.news_min_body_chars:
        pieces = _chunk(base, settings.news_chunk_chars, settings.news_chunk_overlap)
    else:
        pieces = [snippet or body or title]
    pieces = [p for p in pieces if p]
    return [((f"{title}. {p}".strip() if title else p), p) for p in pieces]


def _emit(ev: EvidenceItem, chunk_text: str, idx: int, score: float) -> EvidenceItem:
    """Clone the article evidence as a chunk, preserving its citation/provenance."""
    clone = ev.model_copy(deep=True)
    if clone.citations:
        clone.citations[0].locator["chunk_text"] = chunk_text
        clone.citations[0].locator["chunk_id"] = idx
        clone.citations[0].locator["relevance"] = round(float(score), 4)
    return clone


def retrieve(articles: list[EvidenceItem], query_text: str, *, settings=None,
             embedder: Optional[Callable] = None, anchor_date: Optional[str] = None
             ) -> list[EvidenceItem]:
    """Rank/dedupe article chunks against the query. Returns surviving chunk
    EvidenceItems (best first). `embedder` is any object with
    ``.encode(list[str], normalize_embeddings=True) -> ndarray``; defaults to the
    shared local model. Falls back to recency order when no model is available."""
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()

    _log.debug(
        "fie news_retrieval.retrieve: query=%r articles=%d anchor=%s top_k=%d",
        (query_text or "")[:120], len(articles), anchor_date, settings.news_top_k,
        extra={"component": "News"},
    )

    if not articles:
        _log.debug("fie news_retrieval.retrieve: no articles -> returning empty", extra={"component": "News"})
        return []

    # Log each article headline coming in
    for i, ev in enumerate(articles[:5]):
        loc = ev.citations[0].locator if ev.citations else {}
        _log.debug(
            "  article[%d] source=%r title=%r published=%s snippet_len=%d",
            i, loc.get("source"), (ev.claim or "")[:80],
            loc.get("published_at"), len(loc.get("snippet") or loc.get("content") or ""),
            extra={"component": "News"},
        )
    if len(articles) > 5:
        _log.debug("  ... and %d more articles", len(articles) - 5, extra={"component": "News"})

    # explode articles -> chunks (carry the source article alongside)
    chunks: list[tuple[EvidenceItem, str, str]] = []  # (article, score_text, chunk_text)
    for ev in articles:
        for score_text, chunk_text in _article_chunks(ev, settings):
            chunks.append((ev, score_text, chunk_text))

    _log.debug(
        "fie news_retrieval.retrieve: exploded %d articles -> %d chunks "
        "(chunk_chars=%d overlap=%d)",
        len(articles), len(chunks),
        settings.news_chunk_chars, settings.news_chunk_overlap,
        extra={"component": "News"},
    )

    if not chunks:
        _log.debug("fie news_retrieval.retrieve: all articles empty after chunking -> returning empty",
                   extra={"component": "News"})
        return []

    if embedder is None:
        from app.engines.extraction.services.embeddings import get_embedder
        embedder = get_embedder(settings.embedding_model)

    hl = settings.news_recency_halflife_days

    # --- fallback: no embedding model -> recency order, exact title dedup ---
    if embedder is None:
        _log.debug(
            "fie news_retrieval.retrieve: no embedder (model=%r) -> fallback recency+title-dedup",
            settings.embedding_model,
            extra={"component": "News"},
        )
        seen, out = set(), []
        ranked = sorted(
            chunks,
            key=lambda c: _recency((c[0].citations[0].locator if c[0].citations else {})
                                   .get("published_at"), anchor_date, hl),
            reverse=True)
        for ev, _stext, ctext in ranked:
            key = (ev.claim or "").strip().lower()
            if key in seen:
                _log.debug("  fallback dedup: skipping duplicate title=%r", key[:60],
                           extra={"component": "News"})
                continue
            seen.add(key)
            out.append(_emit(ev, ctext, len(out), 0.0))
            if len(out) >= settings.news_top_k:
                break
        _log.debug(
            "fie news_retrieval.retrieve: fallback kept=%d/%d chunks (top_k=%d)",
            len(out), len(chunks), settings.news_top_k,
            extra={"component": "News"},
        )
        return out

    # --- semantic path ---
    import numpy as np

    texts = [c[1] for c in chunks]
    _log.debug(
        "fie news_retrieval.retrieve: embedder=%s encoding %d chunk texts + 1 query vector",
        type(embedder).__name__, len(texts),
        extra={"component": "News"},
    )
    vecs = np.asarray(embedder.encode(texts, normalize_embeddings=True))
    qv = np.asarray(embedder.encode([query_text or ""], normalize_embeddings=True)[0])

    w = settings.news_recency_weight
    scored = []
    below_floor = 0
    for i, (ev, _stext, ctext) in enumerate(chunks):
        cos = float(np.dot(vecs[i], qv))
        if cos < settings.news_similarity_floor:
            below_floor += 1
            continue
        loc = ev.citations[0].locator if ev.citations else {}
        rec = _recency(loc.get("published_at"), anchor_date, hl)
        scored.append((((1 - w) * cos + w * rec), cos, i, ev, ctext, vecs[i]))
    scored.sort(key=lambda t: t[0], reverse=True)

    _log.debug(
        "fie news_retrieval.retrieve: cosine-scored=%d below_floor=%d "
        "(floor=%.3f recency_weight=%.2f halflife=%dd)",
        len(scored), below_floor,
        settings.news_similarity_floor, w, hl,
        extra={"component": "News"},
    )
    for idx, (blended, cos, ci, ev, ctext, _v) in enumerate(scored[:10]):
        loc = ev.citations[0].locator if ev.citations else {}
        _log.debug(
            "  ranked[%d] cos=%.4f blended=%.4f source=%r published=%s chunk=%r",
            idx, cos, blended,
            loc.get("source"), loc.get("published_at"),
            ctext[:80],
            extra={"component": "News"},
        )

    # Independence-aware dedup: cluster by "same story" similarity and keep one
    # representative per story. A near-identical story carried by several outlets is
    # SYNDICATION (one origin), not independent corroboration — fold the outlets into
    # the representative's `syndicated_in` rather than counting them as separate
    # confirmations (legacy MSIL circular-evidence rule). Each surviving item is then
    # a distinct (independent) story; the count drives a corroboration strength.
    threshold = settings.news_same_story_similarity
    kept_vecs, out = [], []
    syndicated = 0
    for blended, _cos, _i, ev, ctext, v in scored:
        dup_idx = next((j for j, kv in enumerate(kept_vecs)
                        if float(np.dot(v, kv)) >= threshold), None)
        if dup_idx is not None:                       # same story -> fold the outlet
            rep_loc = out[dup_idx].citations[0].locator if out[dup_idx].citations else {}
            src = (ev.citations[0].locator.get("source") if ev.citations else None)
            syn = rep_loc.setdefault("syndicated_in", [rep_loc.get("source")])
            if src and src not in syn:
                syn.append(src)
            syndicated += 1
            _log.debug(
                "  dedup: chunk similar to kept[%d] (threshold=%.2f) -> folded into syndicated_in",
                dup_idx, threshold,
                extra={"component": "News"},
            )
            continue
        kept_vecs.append(v)
        out.append(_emit(ev, ctext, len(out), blended))
        _log.debug(
            "  kept[%d] blended=%.4f chunk=%r",
            len(out) - 1, blended, ctext[:60],
            extra={"component": "News"},
        )
        if len(out) >= settings.news_top_k:
            break

    # corroboration counts INDEPENDENT stories (reps), not articles: n reprints of one
    # wire => 1 independent story => no inflated confidence.
    n = len(out)
    strength = round(1 - 0.5 ** (n - 1), 4) if n else 0.0
    for it in out:
        loc = it.citations[0].locator if it.citations else {}
        loc.setdefault("syndicated_in", [loc.get("source")])
        loc["independent_stories"] = n
        loc["corroboration_strength"] = strength

    _log.debug(
        "fie news_retrieval.retrieve: final kept=%d syndicated=%d "
        "independent_stories=%d corroboration_strength=%.4f",
        n, syndicated, n, strength,
        extra={"component": "News"},
    )
    return out
