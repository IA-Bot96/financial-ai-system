"""Stage 2 — Table & Section detection (rule-based + free local ML).

Flow:
  1. detect_sections()                — heading -> page ranges.
  2. extract raw grids per page       — pdfplumber (native) / OCR word-clustering.
  3. merge_multipage()                — stitch tables that continue across pages.
  4. detect_and_normalize()           — fix vertical/horizontal orientation.
  5. TableClassifier.classify()       — free local fuzzy+embedding; ambiguous
                                         tables flagged needs_review for GPT.

Returns a `TableSet`. Only `pdf_path` + the Layer-1 `IngestedDoc` are required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.common import SourceRef, StatementType
from app.engines.extraction.models.document import IngestedDoc, PageKind
from app.engines.extraction.models.table import RawTable, TableSet
from app.engines.extraction.pipeline import gridutils as gu
from app.engines.extraction.pipeline.sections import detect_sections, section_for_page
from app.engines.extraction.services.classifier import TableClassifier
from app.engines.extraction.services.ocr import OCRService

logger = get_logger(__name__)


@dataclass
class _PageTable:
    """Intermediate per-page table before merge/normalize/classify."""

    pages: list[int]
    rows: list[list[str]]
    section_type: StatementType
    section_title: str = ""
    context_text: str = ""


def _clean_grid(grid: list[list[str]]) -> list[list[str]]:
    cleaned = [[(c or "").strip() for c in row] for row in grid]
    # Drop fully empty rows and trailing empty columns.
    cleaned = [r for r in cleaned if any(r)]
    return gu.rectangularize(cleaned)


# --- per-page extraction ---

def _word_grid(pl_page, settings) -> list[list[str]] | None:
    """Numeric-anchored reconstruction from positioned words."""
    try:
        words = [
            {"text": w["text"], "x0": w["x0"], "x1": w["x1"], "top": w["top"], "bottom": w["bottom"]}
            for w in pl_page.extract_words()
        ]
        cg = _clean_grid(gu.cluster_words_to_grid(words, settings.table_row_tol, settings.table_col_tol))
        if gu.looks_tabular(cg, settings.table_min_numeric_ratio) and gu.has_labels(cg):
            return cg
    except Exception as exc:  # noqa: BLE001
        logger.debug("Word reconstruction failed on page %s: %s", pl_page.page_number, exc)
    return None


def _native_grids(pl_page, settings) -> list[list[list[str]]]:
    """Extract native tables.

    pdfplumber's ruled extraction often captures only the numeric columns and
    drops the row labels on these financial statements, so we accept a ruled
    table only if it actually carries labels; otherwise we fall back to the
    numeric-anchored word reconstruction.
    """
    ruled: list[list[list[str]]] = []
    try:
        for g in pl_page.extract_tables() or []:
            cg = _clean_grid(g)
            if gu.looks_tabular(cg, settings.table_min_numeric_ratio) and gu.has_labels(cg):
                ruled.append(cg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdfplumber table extraction failed on page %s: %s", pl_page.page_number, exc)

    if ruled:
        return ruled

    word_grid = _word_grid(pl_page, settings)
    return [word_grid] if word_grid else []


def _ocr_grids(fitz_page, ocr: OCRService, settings) -> list[list[list[str]]]:
    """Reconstruct a grid from a scanned page via OCR word bounding boxes."""
    try:
        pix = fitz_page.get_pixmap(dpi=settings.ocr_dpi)
        words = ocr.png_bytes_to_words(pix.tobytes("png"))
        g = gu.cluster_words_to_grid(words, settings.table_row_tol, settings.table_col_tol)
        cg = _clean_grid(g)
        if gu.looks_tabular(cg, settings.table_min_numeric_ratio):
            return [cg]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR table extraction failed on page %s: %s", fitz_page.number + 1, exc)
    return []


# --- multipage merge ---

def merge_multipage(page_tables: list[_PageTable]) -> list[_PageTable]:
    merged: list[_PageTable] = []
    for pt in page_tables:
        if merged:
            prev = merged[-1]
            same_section = prev.section_type == pt.section_type
            if gu.is_continuation(prev.rows, prev.pages, pt.rows, pt.pages, same_section):
                prev.rows.extend(pt.rows)  # continuation has no header row
                prev.pages = sorted(set(prev.pages) | set(pt.pages))
                continue
        merged.append(pt)
    return merged


# --- main entry ---

def detect_tables(
    pdf_path: Path,
    doc: IngestedDoc,
    classifier: TableClassifier | None = None,
    ocr: OCRService | None = None,
) -> TableSet:
    settings = get_settings()
    classifier = classifier or TableClassifier()
    ocr = ocr or OCRService()

    sections = detect_sections(doc)
    page_tables: list[_PageTable] = []

    import fitz  # PyMuPDF
    import pdfplumber

    pl = pdfplumber.open(pdf_path)
    fz = fitz.open(pdf_path)
    try:
        for page in doc.pages:
            sec = section_for_page(sections, page.page)
            sec_type = sec.statement_type if sec else StatementType.other
            sec_title = sec.title if sec else ""

            if page.kind == PageKind.native:
                grids = _native_grids(pl.pages[page.page - 1], settings)
            elif page.kind == PageKind.ocr:
                grids = _ocr_grids(fz[page.page - 1], ocr, settings)
            else:
                grids = []

            for grid in grids:
                page_tables.append(
                    _PageTable(
                        pages=[page.page],
                        rows=grid,
                        section_type=sec_type,
                        section_title=sec_title,
                        context_text=page.text[:400],
                    )
                )
    finally:
        pl.close()
        fz.close()

    merged = merge_multipage(page_tables)

    tables: list[RawTable] = []
    for idx, pt in enumerate(merged):
        rows, orientation = gu.detect_and_normalize(pt.rows)
        if len(rows) < 2:
            continue
        header, data = rows[0], rows[1:]
        years = gu.extract_years(header)
        currency, unit = gu.detect_currency_unit(pt.section_title, pt.context_text, " ".join(header))

        labels = " ".join(r[0] for r in data[:15] if r)
        signature = " ".join([pt.section_title, " ".join(header), labels]).strip()
        cls = classifier.classify(signature, section_hint=pt.section_type)

        tables.append(
            RawTable(
                table_id=f"{doc.file_name}::t{idx}",
                statement_type=cls.statement_type,
                title=pt.section_title or (header[0] if header else ""),
                header=header,
                rows=data,
                orientation=orientation,
                years=years,
                currency=currency,
                unit_scale=unit,
                needs_review=cls.needs_review,
                classification_method=cls.method,
                classification_score=round(cls.score, 4),
                source=SourceRef(
                    report_file=doc.file_name,
                    report_year=doc.report_year,
                    pages=pt.pages,
                    section=pt.section_title or None,
                ),
            )
        )

    logger.info(
        "Detected %d tables in %s (%d need GPT review)",
        len(tables), doc.file_name, sum(1 for t in tables if t.needs_review),
    )
    return TableSet(
        file_name=doc.file_name,
        report_year=doc.report_year,
        sections=sections,
        tables=tables,
    )
