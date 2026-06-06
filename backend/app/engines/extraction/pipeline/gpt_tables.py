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


# Bottom-line totals that only a PRIMARY face statement carries (a note/narrative
# page does not). A page with one of these is NEVER skipped by skip_consolidated, even
# when the (heuristic, carry-forward) consolidated context flagged it: dropping a
# standalone primary statement is catastrophic (no face truth), whereas re-extracting a
# consolidated one only costs a GPT call. `build_face_truth` doesn't filter by the
# consolidated flag, so a recovered statement still supplies truth.
_PRIMARY_STMT_RE = re.compile(
    r"profit for the year|total assets|total equity and liabilities|"
    r"total equity & liabilities|profit before tax(?:ation)?|net cash (?:generated|used)",
    re.I,
)


def _looks_primary_statement(text: str) -> bool:
    return bool(_PRIMARY_STMT_RE.search(text or ""))


# Prepended to the user prompt when a page image is attached (vision mode).
_VISION_NOTE = (
    "An image of this page is attached. Treat the IMAGE as the authoritative source: use "
    "it to resolve any OCR ambiguity in the text below — exact digits, negative signs and "
    "parentheses, decimal points, column/year alignment, merged or multi-line cells, and "
    "the consolidated vs unconsolidated heading. Read exact figures from the page text "
    "where it agrees with the image; trust the image when they conflict.\n\n"
)


def _render_page_png(pdf, page_number: int, dpi: int) -> bytes:
    """Rasterize a 1-based page of an OPEN fitz document to PNG bytes."""
    return pdf[page_number - 1].get_pixmap(dpi=dpi).tobytes("png")


def _candidate_pages(doc: IngestedDoc, context: dict, region_start: int,
                     skip_consolidated: bool, min_money: int, dense_digits: int) -> list:
    """Financial pages to send to GPT. On template runs the consolidated set is skipped
    to halve calls — EXCEPT pages carrying a primary face-statement total, which are
    always kept (the consolidated context is a fragile heuristic; never let it drop a
    standalone primary statement)."""
    return [
        p for p in doc.pages
        if p.page >= region_start
        and _is_financial_page(p.text, min_money, dense_digits)
        and not (skip_consolidated and context.get(p.page) is True
                 and not _looks_primary_statement(p.text))
    ]


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
    """Resolve each line to a canonical metric — SCOPED by the table's statement
    family (P2). A label that resolves to a metric in a different family than its
    home table (e.g. a 'Cost of sales' allocation row inside a PP&E note) is
    DEMOTED to no_confident_metric rather than mis-tagged as the headline metric."""
    from app.engines.extraction.services.face_truth import confidently_incompatible
    for li in table.line_items:
        match = resolver.resolve(li.label)
        if not match:
            continue
        li.canonical_metric = match.canonical_key
        li.canonical_category = match.category
        if confidently_incompatible(li, table.statement_type):
            li.canonical_metric = None
            li.canonical_category = None
            li.resolution = "no_confident_metric"


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
        page_text=rows_text, vision_note="",
    )
    try:
        result = gpt.complete_structured(system, user, FinancialTableList)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GPT grid structuring failed for %s: %s", raw.table_id, exc)
        return None

    from app.engines.extraction.services.face_truth import infer_table_role
    for table in result.tables:
        if not table.line_items:
            continue
        table.source = raw.source
        table.consolidated = raw.consolidated
        if not table.years:
            table.years = sorted({v.year for li in table.line_items for v in li.values if v.year})
        _resolve_canonicals(table, resolver)
        table.table_role = table.table_role or infer_table_role(table)  # C2/P3
        from app.engines.extraction.services.cell_parse import normalize_table_values
        norm = normalize_table_values(table)       # harden signs/note-refs from raw (complements GPT)
        if norm.get("sign_fixed") or norm.get("note_ref_dropped"):
            logger.info("normalize grid %s: sign_fixed=%d note_ref_dropped=%d", raw.table_id,
                        norm.get("sign_fixed", 0), norm.get("note_ref_dropped", 0))
        return table
    return None


def extract_financial_tables(doc: IngestedDoc, gpt, skip_consolidated: bool = False,
                             pdf_path=None) -> list[FinancialTable]:
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

    candidates = _candidate_pages(
        doc, context, region_start, skip_consolidated,
        settings.gpt_table_min_money, settings.gpt_table_dense_digits,
    )
    kept_primary = sum(1 for p in candidates
                       if skip_consolidated and context.get(p.page) is True)
    if kept_primary:
        logger.info("Kept %d consolidated-flagged page(s) that carry a primary-statement "
                    "total (not skipped) in %s", kept_primary, doc.file_name)
    if len(candidates) > settings.gpt_table_max_pages:
        dropped = [p.page for p in candidates[settings.gpt_table_max_pages:]]
        # Disclose EXACTLY which pages are skipped — a capped page that held a needed
        # statement/restated note would otherwise look like an extraction gap, not a
        # cap. (Surfaced so the run is auditable; raise gpt_table_max_pages to include.)
        logger.warning(
            "PAGE CAP HIT in %s: extracting %d of %d financial pages; SKIPPING pages %s "
            "(data on these pages will be absent — raise gpt_table_max_pages to include).",
            doc.file_name, settings.gpt_table_max_pages, len(candidates), dropped,
        )
        candidates = candidates[: settings.gpt_table_max_pages]

    total = len(candidates)
    workers = max(1, min(settings.gpt_table_workers, total))

    # Vision: render an image of each candidate page (once, in the main thread — fitz
    # Documents aren't thread-safe) so GPT can read the page, not just its lossy OCR.
    page_images: dict[int, bytes] = {}
    if settings.use_vision_extraction and pdf_path:
        cap = settings.vision_max_pages or total
        try:
            import fitz  # PyMuPDF
            pdf = fitz.open(pdf_path)
            try:
                for p in candidates[:cap]:
                    page_images[p.page] = _render_page_png(pdf, p.page, settings.vision_dpi)
            finally:
                pdf.close()
            logger.info("Vision: attached %d page image(s) (dpi=%d) in %s",
                        len(page_images), settings.vision_dpi, doc.file_name)
        except Exception as exc:  # noqa: BLE001 — never let rendering break extraction
            logger.warning("Vision rendering failed in %s (%s); text-only fallback.",
                           doc.file_name, exc)
            page_images = {}

    logger.info(
        "GPT table extraction: %d financial page(s) from p%d onward in %s "
        "(skip_consolidated=%s, workers=%d, vision=%s)",
        total, region_start, doc.file_name, skip_consolidated, workers, bool(page_images),
    )

    def _call(page):
        imgs = [page_images[page.page]] if page.page in page_images else None
        system, user = prompts.render(
            "extract_tables", allowed_types=_ALLOWED,
            report_file=doc.file_name, report_year=doc.report_year, page=page.page,
            page_text=page.text, vision_note=(_VISION_NOTE if imgs else ""),
        )
        try:
            return page, gpt.complete_structured(system, user, FinancialTableList, images=imgs)
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
    from app.engines.extraction.services.face_truth import infer_table_role
    for page, result in sorted(results, key=lambda pr: pr[0].page):
        if result is None:
            continue
        for seq, table in enumerate(result.tables):
            if not table.line_items:
                continue
            table.source = SourceRef(
                report_file=doc.file_name, report_year=doc.report_year, pages=[page.page],
                table_id=f"{doc.file_name}:p{page.page}:{seq}", table_title=table.title or None,
            )
            table.consolidated = context.get(page.page)
            if not table.years:
                table.years = sorted({v.year for li in table.line_items for v in li.values if v.year})
            _resolve_canonicals(table, resolver)
            table.table_role = table.table_role or infer_table_role(table)  # C2/P3
            from app.engines.extraction.services.cell_parse import normalize_table_values
            norm = normalize_table_values(table)   # harden signs/note-refs from raw (complements GPT)
            if norm.get("sign_fixed") or norm.get("note_ref_dropped"):
                logger.info("normalize p%d %r: sign_fixed=%d note_ref_dropped=%d",
                            page.page, (table.title or "")[:40],
                            norm.get("sign_fixed", 0), norm.get("note_ref_dropped", 0))
            out.append(table)

    logger.info("GPT extracted %d financial table(s) from %d page(s) in %s",
                len(out), total, doc.file_name)
    return out
