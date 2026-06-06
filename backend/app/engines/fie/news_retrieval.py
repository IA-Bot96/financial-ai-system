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

from datetime import date
from typing import Callable, Optional

from .models import EvidenceItem


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
    if not articles:
        return []

    # explode articles -> chunks (carry the source article alongside)
    chunks: list[tuple[EvidenceItem, str, str]] = []  # (article, score_text, chunk_text)
    for ev in articles:
        for score_text, chunk_text in _article_chunks(ev, settings):
            chunks.append((ev, score_text, chunk_text))
    if not chunks:
        return []

    if embedder is None:
        from app.engines.extraction.services.embeddings import get_embedder
        embedder = get_embedder(settings.embedding_model)

    hl = settings.news_recency_halflife_days

    # --- fallback: no embedding model -> recency order, exact title dedup ---
    if embedder is None:
        seen, out = set(), []
        ranked = sorted(
            chunks,
            key=lambda c: _recency((c[0].citations[0].locator if c[0].citations else {})
                                   .get("published_at"), anchor_date, hl),
            reverse=True)
        for ev, _stext, ctext in ranked:
            key = (ev.claim or "").strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(_emit(ev, ctext, len(out), 0.0))
            if len(out) >= settings.news_top_k:
                break
        return out

    # --- semantic path ---
    import numpy as np

    texts = [c[1] for c in chunks]
    vecs = np.asarray(embedder.encode(texts, normalize_embeddings=True))
    qv = np.asarray(embedder.encode([query_text or ""], normalize_embeddings=True)[0])

    w = settings.news_recency_weight
    scored = []
    for i, (ev, _stext, ctext) in enumerate(chunks):
        cos = float(np.dot(vecs[i], qv))
        if cos < settings.news_similarity_floor:
            continue
        loc = ev.citations[0].locator if ev.citations else {}
        rec = _recency(loc.get("published_at"), anchor_date, hl)
        scored.append((((1 - w) * cos + w * rec), cos, i, ev, ctext, vecs[i]))
    scored.sort(key=lambda t: t[0], reverse=True)

    # Independence-aware dedup: cluster by "same story" similarity and keep one
    # representative per story. A near-identical story carried by several outlets is
    # SYNDICATION (one origin), not independent corroboration — fold the outlets into
    # the representative's `syndicated_in` rather than counting them as separate
    # confirmations (legacy MSIL circular-evidence rule). Each surviving item is then
    # a distinct (independent) story; the count drives a corroboration strength.
    threshold = settings.news_same_story_similarity
    kept_vecs, out = [], []
    for blended, _cos, _i, ev, ctext, v in scored:
        dup_idx = next((j for j, kv in enumerate(kept_vecs)
                        if float(np.dot(v, kv)) >= threshold), None)
        if dup_idx is not None:                       # same story -> fold the outlet
            rep_loc = out[dup_idx].citations[0].locator if out[dup_idx].citations else {}
            src = (ev.citations[0].locator.get("source") if ev.citations else None)
            syn = rep_loc.setdefault("syndicated_in", [rep_loc.get("source")])
            if src and src not in syn:
                syn.append(src)
            continue
        kept_vecs.append(v)
        out.append(_emit(ev, ctext, len(out), blended))
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
    return out
