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

# Company name = a capitalized phrase ending in a legal/business suffix.
_LEGAL_SUFFIX = (
    r"(?:Limited|Ltd\.?|PLC|P\.?L\.?C\.?|Corporation|Industries|Holdings|"
    r"Mills|Group|Incorporated|Inc\.?|Company)"
)
_COMPANY_RE = re.compile(r"([A-Z][A-Za-z0-9&.,'()\-]*(?:\s+[A-Za-z0-9&.,'()\-]+){1,5}?\s+" + _LEGAL_SUFFIX + r")\b")
_COMPANY_STOPWORDS = {"the company", "our company", "holding company", "the group", "the holding company", "group company"}


def _extract_native_text(page) -> str:
    """Native text-layer extraction for a PyMuPDF page."""
    try:
        return page.get_text("text") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Native text extraction failed on page %s: %s", page.number + 1, exc)
        return ""


def _ocr_page(page, ocr: OCRService, dpi: int) -> tuple[str, float | None, list[dict]]:
    """Rasterize a page to PNG and OCR it ONCE. Returns (text, confidence, words).

    The word boxes are cached on the page so table detection (Layer 2) can reuse
    them instead of re-rasterizing and re-OCRing the same scanned page.
    """
    try:
        pix = page.get_pixmap(dpi=dpi)
        result = ocr.png_bytes_to_page(pix.tobytes("png"))
        return result.text, result.confidence, result.words
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR failed on page %s: %s", page.number + 1, exc)
        return "", None, []


def _infer_year(file_name: str, cover_text: str) -> int | None:
    """Infer fiscal year from the DOCUMENT first (cover pages), filename only as
    a fallback. The document is the source of truth even if the filename has a
    year. Picks the largest plausible 4-digit year found.
    """
    for source in (cover_text, file_name):  # document truth first
        years = [
            int(m.group())
            for m in _YEAR_RE.finditer(source)
            if _MIN_PLAUSIBLE_YEAR <= int(m.group()) <= _MAX_PLAUSIBLE_YEAR
        ]
        if years:
            return max(years)
    return None


def _infer_company(page_texts: list[str], file_name: str = "") -> str | None:
    """Extract the company name from the document (recurring header/cover line).

    Candidates are capitalized phrases ending in a legal suffix (… Limited / PLC
    / Corporation). The name recurs in page headers, so we pick the most frequent
    candidate (earliest page breaks ties). Document is the source of truth;
    filename is a last resort.
    """
    counts: dict[str, int] = {}
    first_seen: dict[str, tuple[int, str]] = {}
    for idx, text in enumerate(page_texts):
        for line in text.splitlines():
            for m in _COMPANY_RE.finditer(line):
                cand = re.sub(r"\s+", " ", m.group(1)).strip(" .,-")
                if not (2 <= len(cand.split()) <= 6) or cand.lower() in _COMPANY_STOPWORDS:
                    continue
                key = cand.lower()
                counts[key] = counts.get(key, 0) + 1
                first_seen.setdefault(key, (idx, cand))
    if counts:
        best = max(counts, key=lambda k: (counts[k], -first_seen[k][0]))
        return first_seen[best][1]

    # Fallback: filename with year/digits/separators stripped.
    cleaned = re.sub(r"[_\-]+", " ", re.sub(r"\.pdf$", "", file_name, flags=re.I))
    cleaned = re.sub(r"\b(19|20)\d{2}\b", "", cleaned).strip()
    return cleaned.title() or None


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
            ocr_words: list[dict] = []

            if len(native.strip()) >= settings.min_text_chars:
                text, kind, conf = native, PageKind.native, None
            else:
                # Looks scanned/image-only -> OCR fallback (text + word boxes once).
                ocr_text, conf, words = _ocr_page(page, ocr, settings.ocr_dpi)
                if len(ocr_text.strip()) > len(native.strip()):
                    text, kind, ocr_words = ocr_text, PageKind.ocr, words
                else:
                    text, kind, conf = native, PageKind.empty, None

            doc.pages.append(
                PageText(
                    page=page.number + 1,
                    text=text,
                    kind=kind,
                    char_count=len(text),
                    ocr_confidence=conf,
                    ocr_words=ocr_words,
                )
            )
    finally:
        pdf.close()

    doc.page_count = len(doc.pages)
    cover_text = "\n".join(p.text for p in doc.pages[:5])
    doc.report_year = _infer_year(pdf_path.name, cover_text)
    # Scan all pages: the reporting entity recurs in every statement-page header,
    # so frequency reliably picks it over subsidiaries / cover-page mentions.
    doc.company = _infer_company([p.text for p in doc.pages], pdf_path.name)
    doc.is_scanned = doc.page_count > 0 and doc.ocr_page_count > doc.page_count / 2

    logger.info(
        "Ingested %s: %d pages (%d OCR), company=%r, year=%s, scanned=%s",
        pdf_path.name, doc.page_count, doc.ocr_page_count, doc.company, doc.report_year, doc.is_scanned,
    )
    return doc


def ingest_pdfs(pdf_paths: list[Path]) -> list[IngestedDoc]:
    """Ingest multiple PDFs, sharing a single OCRService instance."""
    ocr = OCRService()
    return [ingest_pdf(p, ocr=ocr) for p in pdf_paths]
