"""Interpretation — Insights via section-aware, sliding-window extraction.

Pipeline (mirrors the proven OCR-v2 design, with semantic dedup added):
  narrative sections -> sliding-window chunks -> rule-based ranking
  -> batched GPT calls -> provenance filter -> dedup -> confidence buckets.

Returns (exported, review): exported >= review_threshold; review in
[reject_threshold, review_threshold). Below reject_threshold is dropped.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.document import IngestedDoc
from app.engines.extraction.models.insight import Insight, InsightList
from app.engines.extraction.pipeline.insight_chunks import (
    NarrativeChunk,
    build_chunks,
    rank_chunks,
)
from app.engines.extraction.pipeline.narrative_sections import identify_narrative_sections
from app.engines.extraction.services.insight_dedup import dedup_insights

logger = get_logger(__name__)

_SYSTEM = (
    "You extract concise, actionable business insights from annual-report "
    "narrative text. Return JSON only. Do not summarize the whole report. Every "
    "insight must be directly supported by the provided source chunk, and must "
    "cite the chunk's exact source_section and page_number."
)


def _normalize_section(s: str | None) -> str:
    return " ".join((s or "").strip().lower().split())


def _batched(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _build_user(chunks: list[NarrativeChunk], report_year: int | None) -> str:
    listing = "\n\n".join(
        f"Chunk {i}\nsource_section: {c.source_section}\npage_number: {c.page_number}\n"
        f"text:\n{c.text}"
        for i, c in enumerate(chunks, start=1)
    )
    return (
        "Extract distinct business insights from the chunks below — expansion, "
        "debt/capital, capacity, exports, cost/margin drivers, working capital, "
        "regulatory impact, risks, opportunities, and outlook. Do not force an "
        "insight from boilerplate.\n\n"
        "For each insight set: area (short theme), takeaway (one factual "
        "sentence), source_section and page_number (copied from the chunk it came "
        f"from), year ({report_year}), and confidence 0.0-1.0.\n\n"
        f'Return JSON: {{ "insights": [ ... ] }}\n\nChunks:\n{listing}'
    )


def extract_insights(doc: IngestedDoc, gpt) -> tuple[list[Insight], list[Insight]]:
    """Return (exported, review) insight lists for one document."""
    settings = get_settings()

    pages = identify_narrative_sections(doc)
    chunks = build_chunks(pages, settings.insights_chunk_max_chars, settings.insights_chunk_overlap)
    ranked = rank_chunks(chunks, settings.insights_max_chunks)
    if not ranked:
        logger.info("No narrative chunks for insights in %s", doc.file_name)
        return [], []

    valid_sources = {(c.page_number, _normalize_section(c.source_section)) for c in ranked}

    collected: list[Insight] = []
    calls = 0
    for batch in _batched(ranked, settings.insights_chunks_per_call):
        calls += 1
        try:
            result = gpt.complete_structured(_SYSTEM, _build_user(batch, doc.report_year), InsightList)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Insight batch %d failed for %s: %s", calls, doc.file_name, exc)
            continue
        for ins in result.insights:
            ins.source_report_year = doc.report_year
            if ins.year is None:
                ins.year = doc.report_year
            # Provenance: the cited (page, section) must be a chunk we sent.
            if (ins.page, _normalize_section(ins.source_section)) not in valid_sources:
                logger.debug("Rejected insight with unverifiable source p%s/%s", ins.page, ins.source_section)
                continue
            collected.append(ins)

    deduped = dedup_insights(collected)

    reject, review = settings.insight_reject_threshold, settings.insight_review_threshold
    exported = [i for i in deduped if i.confidence >= review]
    review_bucket = [i for i in deduped if reject <= i.confidence < review]
    logger.info(
        "Insights for %s: %d calls, %d collected -> %d deduped (%d export, %d review)",
        doc.file_name, calls, len(collected), len(deduped), len(exported), len(review_bucket),
    )
    return exported, review_bucket
