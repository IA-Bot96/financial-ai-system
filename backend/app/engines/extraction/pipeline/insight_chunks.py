"""Sliding-window chunking + rule-based ranking for narrative insight chunks."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from app.engines.extraction.pipeline.narrative_sections import NarrativePage
from app.engines.extraction.services.narrative_keywords import (
    FINANCIAL_KEYWORDS,
    SECTION_ORDER,
    SECTION_WEIGHTS,
)


@dataclass(frozen=True)
class NarrativeChunk:
    page_number: int
    source_section: str
    text: str
    score: float = 0.0


def _clean(text: str) -> str:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def build_chunks(pages: list[NarrativePage], max_chars: int, overlap: int) -> list[NarrativeChunk]:
    """Sliding-window chunks with overlap, packing paragraphs and never crossing
    a page/section boundary (so page/section provenance stays exact)."""
    chunks: list[NarrativeChunk] = []
    for page in pages:
        text = _clean(page.text)
        if not text:
            continue
        for piece in _split(text, max_chars, overlap):
            chunks.append(NarrativeChunk(page.page_number, page.section, piece))
    return chunks


def _split(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 1 > max_chars:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n{para}".strip()
        else:
            current = f"{current}\n{para}".strip() if current else para
        # Hard-split an over-long paragraph.
        while len(current) > max_chars:
            chunks.append(current[:max_chars].strip())
            current = current[max_chars - overlap:].strip() if overlap else current[max_chars:].strip()
    if current:
        chunks.append(current)
    return chunks


def _score(chunk: NarrativeChunk) -> float:
    text = chunk.text.lower()
    score = SECTION_WEIGHTS.get(chunk.source_section, 1.0)
    score += 1.5 * sum(1 for kw in FINANCIAL_KEYWORDS if kw in text)
    return score


def rank_chunks(chunks: list[NarrativeChunk], max_chunks: int | None = None) -> list[NarrativeChunk]:
    """Score chunks and return them in section-balanced (round-robin) order."""
    scored = [
        NarrativeChunk(c.page_number, c.source_section, c.text, _score(c))
        for c in chunks
    ]
    by_section: dict[str, list[NarrativeChunk]] = defaultdict(list)
    for c in scored:
        by_section[c.source_section].append(c)
    for items in by_section.values():
        items.sort(key=lambda c: c.score, reverse=True)

    order = [s for s in SECTION_ORDER if s in by_section]
    order += sorted(s for s in by_section if s not in order)

    ordered: list[NarrativeChunk] = []
    while any(by_section[s] for s in order):
        for s in order:
            if by_section[s]:
                ordered.append(by_section[s].pop(0))

    return ordered[:max_chunks] if max_chunks else ordered
