"""GPT-assisted extraction of financial statements/notes from page text.

Rule-based OCR-grid reconstruction fails on scanned/complex pages (garbled years,
prose mis-read as tables). For pages that look financial, we instead send the
page TEXT to GPT and let it return structured FinancialTables — far more robust
to OCR noise and multi-column layouts. Each extracted table gets its source,
consolidated flag, and canonical-metric tags attached locally (no extra cost).
"""
from __future__ import annotations

import re

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.common import SourceRef, StatementType
from app.engines.extraction.models.document import IngestedDoc
from app.engines.extraction.models.financials import FinancialTable, FinancialTableList
from app.engines.extraction.pipeline.sections import (
    consolidated_context_by_page,
    detect_sections,
)
from app.engines.extraction.services import prompts

logger = get_logger(__name__)

_ALLOWED = ", ".join(st.value for st in StatementType)
# Strong financial-table signals (statement/note language + reporting units).
_SIGNALS = (
    "rupees", "pkr", "'000", "in thousand", "in million", "statement of",
    "note", "total ", "revenue", "cost of", "assets", "liabilities", "equity",
    "cash flow", "profit", "depreciation", "balance sheet",
)
# Comma-grouped money figures, e.g. 1,234 or 53,347,603 — the hallmark of a table.
_MONEY_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")


def _is_financial_page(text: str, min_money: int, dense_digits: int) -> bool:
    """Tight gate: a real financial table = a strong signal AND either many
    comma-grouped figures, or (for OCR pages that lose commas) a very digit-dense
    page. Narrative/governance pages with a few numbers are rejected."""
    if not text:
        return False
    low = text.lower()
    if not any(s in low for s in _SIGNALS):
        return False
    if len(_MONEY_RE.findall(text)) >= min_money:
        return True
    return sum(c.isdigit() for c in text) >= dense_digits


def _financial_region_start(doc: IngestedDoc) -> int:
    """First page of the financial-statements block (front-matter narrative,
    MD&A, governance — which precede it — are excluded). 1 if undetectable."""
    sections = detect_sections(doc)
    return min((s.start_page for s in sections), default=1)


def _resolve_canonicals(table: FinancialTable, resolver) -> None:
    for li in table.line_items:
        match = resolver.resolve(li.label)
        if match:
            li.canonical_metric = match.canonical_key
            li.canonical_category = match.category


def gpt_structure_grid(raw, gpt) -> FinancialTable | None:
    """Classify AND reconstruct a single unclassified native grid in one GPT call.

    Returns a FinancialTable if GPT judges the grid a financial table, else None.
    (Add-on for native leftovers; scanned pages are handled by the page-text path.)
    """
    from app.engines.extraction.services.metric_resolver import get_resolver

    resolver = get_resolver()
    rows_text = "\n".join(
        "\t".join((c or "") for c in row) for row in ([raw.header] + list(raw.rows))
    )
    system, user = prompts.render(
        "extract_tables", allowed_types=_ALLOWED,
        report_file=(raw.source.report_file if raw.source else ""),
        report_year=(raw.source.report_year if raw.source else ""),
        page=(raw.source.pages if raw.source else []),
        page_text=rows_text,
    )
    try:
        result = gpt.complete_structured(system, user, FinancialTableList)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GPT grid structuring failed for %s: %s", raw.table_id, exc)
        return None

    for table in result.tables:
        if not table.line_items:
            continue
        table.source = raw.source
        table.consolidated = raw.consolidated
        if not table.years:
            table.years = sorted({v.year for li in table.line_items for v in li.values if v.year})
        _resolve_canonicals(table, resolver)
        return table
    return None


def extract_financial_tables(doc: IngestedDoc, gpt, skip_consolidated: bool = False) -> list[FinancialTable]:
    """Extract financial tables from the document's financial pages via GPT.

    Scoped tightly to keep GPT calls low:
      - only pages in the financial-statements block (front matter excluded),
      - only pages that look like real money tables (see `_is_financial_page`),
      - on template runs (`skip_consolidated`), the consolidated set is skipped
        (the template targets unconsolidated), halving calls.
    Logs per-page progress so the run is visibly moving.
    """
    settings = get_settings()
    from app.engines.extraction.services.metric_resolver import get_resolver

    resolver = get_resolver()
    context = consolidated_context_by_page(doc)
    region_start = _financial_region_start(doc)

    candidates = [
        p for p in doc.pages
        if p.page >= region_start
        and _is_financial_page(p.text, settings.gpt_table_min_money, settings.gpt_table_dense_digits)
        and not (skip_consolidated and context.get(p.page) is True)
    ]
    if len(candidates) > settings.gpt_table_max_pages:
        logger.warning("Capping GPT table extraction at %d of %d financial pages in %s",
                       settings.gpt_table_max_pages, len(candidates), doc.file_name)
        candidates = candidates[: settings.gpt_table_max_pages]

    total = len(candidates)
    workers = max(1, min(settings.gpt_table_workers, total))
    logger.info(
        "GPT table extraction: %d financial page(s) from p%d onward in %s "
        "(skip_consolidated=%s, workers=%d)",
        total, region_start, doc.file_name, skip_consolidated, workers,
    )

    def _call(page):
        system, user = prompts.render(
            "extract_tables", allowed_types=_ALLOWED,
            report_file=doc.file_name, report_year=doc.report_year, page=page.page,
            page_text=page.text,
        )
        try:
            return page, gpt.complete_structured(system, user, FinancialTableList)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GPT table extraction failed on page %s: %s", page.page, exc)
            return page, None

    # Pages are independent -> run the GPT calls concurrently (the bottleneck).
    results: list[tuple] = []
    if workers == 1:
        for i, page in enumerate(candidates, start=1):
            results.append(_call(page))
            logger.info("  GPT page %d/%d (p%d)", i, total, page.page)
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_call, p) for p in candidates]
            for done, fut in enumerate(as_completed(futures), start=1):
                page, _ = result_pair = fut.result()
                results.append(result_pair)
                logger.info("  GPT page %d/%d done (p%d)", done, total, page.page)

    # Post-process in deterministic page order (cheap; keeps output stable).
    out: list[FinancialTable] = []
    for page, result in sorted(results, key=lambda pr: pr[0].page):
        if result is None:
            continue
        for table in result.tables:
            if not table.line_items:
                continue
            table.source = SourceRef(
                report_file=doc.file_name, report_year=doc.report_year, pages=[page.page],
            )
            table.consolidated = context.get(page.page)
            if not table.years:
                table.years = sorted({v.year for li in table.line_items for v in li.values if v.year})
            _resolve_canonicals(table, resolver)
            out.append(table)

    logger.info("GPT extracted %d financial table(s) from %d page(s) in %s",
                len(out), total, doc.file_name)
    return out
