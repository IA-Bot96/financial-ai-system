"""Stage 1 — Ingest / OCR (rule-based).

Responsibilities:
  1. Open a PDF and read its native text layer per page (PyMuPDF).
  2. Detect image-only / scanned pages by a character-count threshold and OCR
     them as a fallback (tesseract via OCRService).
  3. Infer the report's fiscal year from the filename, then the cover pages.
  4. Return a structured `IngestedDoc` with per-page provenance.

The rules (thresholds, DPI, language) live in core.config.Settings.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.services.ocr import OCRService

logger = get_logger(__name__)

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_MIN_PLAUSIBLE_YEAR = 1990
_MAX_PLAUSIBLE_YEAR = 2100


def _extract_native_text(page) -> str:
    """Native text-layer extraction for a PyMuPDF page."""
    try:
        return page.get_text("text") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Native text extraction failed on page %s: %s", page.number + 1, exc)
        return ""


def _ocr_page(page, ocr: OCRService, dpi: int) -> tuple[str, float | None]:
    """Rasterize a page to PNG and OCR it. Returns (text, confidence)."""
    try:
        pix = page.get_pixmap(dpi=dpi)
        result = ocr.png_bytes_to_text(pix.tobytes("png"))
        return result.text, result.confidence
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR failed on page %s: %s", page.number + 1, exc)
        return "", None


def _infer_year(file_name: str, cover_text: str) -> int | None:
    """Infer fiscal year: prefer the filename, then fall back to cover-page text.

    Picks the largest plausible 4-digit year found.
    """
    for source in (file_name, cover_text):
        years = [
            int(m.group())
            for m in _YEAR_RE.finditer(source)
            if _MIN_PLAUSIBLE_YEAR <= int(m.group()) <= _MAX_PLAUSIBLE_YEAR
        ]
        if years:
            return max(years)
    return None


def ingest_pdf(pdf_path: Path, ocr: OCRService | None = None) -> IngestedDoc:
    """Ingest a single PDF into an `IngestedDoc`.

    Args:
        pdf_path: Path to the PDF.
        ocr: Optional OCRService (injected for testing); created on demand.

    Raises:
        FileNotFoundError: if the PDF does not exist.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    import fitz  # PyMuPDF

    settings = get_settings()
    ocr = ocr or OCRService()
    doc = IngestedDoc(file_name=pdf_path.name)

    pdf = fitz.open(pdf_path)
    try:
        for page in pdf:
            native = _extract_native_text(page)

            if len(native.strip()) >= settings.min_text_chars:
                text, kind, conf = native, PageKind.native, None
            else:
                # Looks scanned/image-only -> OCR fallback.
                ocr_text, conf = _ocr_page(page, ocr, settings.ocr_dpi)
                if len(ocr_text.strip()) > len(native.strip()):
                    text, kind = ocr_text, PageKind.ocr
                else:
                    text, kind, conf = native, PageKind.empty, None

            doc.pages.append(
                PageText(
                    page=page.number + 1,
                    text=text,
                    kind=kind,
                    char_count=len(text),
                    ocr_confidence=conf,
                )
            )
    finally:
        pdf.close()

    doc.page_count = len(doc.pages)
    cover_text = "\n".join(p.text for p in doc.pages[:3])
    doc.report_year = _infer_year(pdf_path.name, cover_text)
    doc.is_scanned = doc.page_count > 0 and doc.ocr_page_count > doc.page_count / 2

    logger.info(
        "Ingested %s: %d pages (%d OCR), year=%s, scanned=%s",
        pdf_path.name, doc.page_count, doc.ocr_page_count, doc.report_year, doc.is_scanned,
    )
    return doc


def ingest_pdfs(pdf_paths: list[Path]) -> list[IngestedDoc]:
    """Ingest multiple PDFs, sharing a single OCRService instance."""
    ocr = OCRService()
    return [ingest_pdf(p, ocr=ocr) for p in pdf_paths]
