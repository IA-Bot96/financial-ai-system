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


# --- Parallel OCR worker (runs in a separate process) -------------------------
# Each worker caches its own fitz Document(s) and OCRService, so nothing
# non-picklable or thread-unsafe ever crosses the process boundary.
_WORKER_DOCS: dict[str, object] = {}
_WORKER_OCR: OCRService | None = None


def _ocr_page_task(task: tuple[str, int, int]) -> tuple[str, int, str, float | None, list[dict]]:
    """OCR one (pdf, page) in a worker process -> (pdf_path, page_no, text, conf,
    words). Render/OCR errors degrade to empty text inside `_ocr_page`."""
    pdf_path, page_no, dpi = task
    import fitz  # PyMuPDF

    global _WORKER_OCR
    doc = _WORKER_DOCS.get(pdf_path)
    if doc is None:
        doc = fitz.open(pdf_path)
        _WORKER_DOCS[pdf_path] = doc
    if _WORKER_OCR is None:
        _WORKER_OCR = OCRService()
    text, conf, words = _ocr_page(doc[page_no], _WORKER_OCR, dpi)
    return pdf_path, page_no, text, conf, words


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


def _build_page_text(page_no: int, native: str,
                     ocr_result: tuple[str, float | None, list[dict]] | None) -> PageText:
    """Assemble one PageText from native text and an optional OCR result, applying
    the SAME native-vs-OCR decision the serial path always used: OCR wins only if
    it yields more text than the native layer; otherwise the page stays native (or,
    after a failed/empty OCR fallback, is marked empty)."""
    if ocr_result is None:
        return PageText(page=page_no, text=native, kind=PageKind.native,
                        char_count=len(native), ocr_confidence=None, ocr_words=[])
    ocr_text, conf, words = ocr_result
    if len(ocr_text.strip()) > len(native.strip()):
        return PageText(page=page_no, text=ocr_text, kind=PageKind.ocr,
                        char_count=len(ocr_text), ocr_confidence=conf, ocr_words=words)
    return PageText(page=page_no, text=native, kind=PageKind.empty,
                    char_count=len(native), ocr_confidence=None, ocr_words=[])


def _finalize_doc(doc: IngestedDoc, pdf_path: Path) -> IngestedDoc:
    """Populate page_count, inferred year/company, scanned flag; log a summary.
    Scan all pages: the reporting entity recurs in every statement-page header, so
    frequency reliably picks it over subsidiaries / cover-page mentions."""
    doc.page_count = len(doc.pages)
    cover_text = "\n".join(p.text for p in doc.pages[:5])
    doc.report_year = _infer_year(pdf_path.name, cover_text)
    doc.company = _infer_company([p.text for p in doc.pages], pdf_path.name)
    doc.is_scanned = doc.page_count > 0 and doc.ocr_page_count > doc.page_count / 2
    logger.info(
        "Ingested %s: %d pages (%d OCR), company=%r, year=%s, scanned=%s",
        pdf_path.name, doc.page_count, doc.ocr_page_count, doc.company, doc.report_year, doc.is_scanned,
    )
    return doc


def ingest_pdf(pdf_path: Path, ocr: OCRService | None = None) -> IngestedDoc:
    """Ingest a single PDF into an `IngestedDoc` (serial, in-process OCR).

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
                ocr_result = None
            else:
                # Looks scanned/image-only -> OCR fallback (text + word boxes once).
                ocr_result = _ocr_page(page, ocr, settings.ocr_dpi)
            doc.pages.append(_build_page_text(page.number + 1, native, ocr_result))
    finally:
        pdf.close()

    return _finalize_doc(doc, pdf_path)


def _resolve_ocr_workers(configured: int, n_tasks: int) -> int:
    """Worker count for the OCR pool. 0 => auto (cpu_count - 1); never exceed the
    number of OCR tasks; <=1 task is always serial (pool overhead isn't worth it)."""
    if n_tasks <= 1:
        return 1
    if configured and configured > 0:
        cap = configured
    else:
        import os
        cap = max(1, (os.cpu_count() or 2) - 1)
    return max(1, min(cap, n_tasks))


def ingest_pdfs(pdf_paths: list[Path]) -> list[tuple[Path, IngestedDoc]]:
    """Ingest multiple PDFs, OCR'ing every scanned page of every PDF through ONE
    flat process pool — page-level AND document-level parallelism under a single
    core budget (no nested pools, no oversubscription).

    Output is identical to serial ingest: pages are reassembled by (doc, page)
    order regardless of the order workers finish in (Phase C is shared with the
    serial path). A PDF that can't be opened is logged and skipped — the batch
    survives. Returns (path, doc) pairs for the PDFs that ingested successfully."""
    settings = get_settings()
    paths = [Path(p) for p in pdf_paths]

    import fitz  # PyMuPDF

    # Phase A (main process, cheap): native text + scan classification; build the
    # flat OCR work-list spanning ALL documents.
    natives: dict[int, list[str]] = {}        # doc_idx -> [native text per page]
    ok_paths: dict[int, Path] = {}            # doc_idx -> path (opened successfully)
    tasks: list[tuple[str, int, int]] = []    # (pdf_path, page0, dpi)
    task_owner: list[tuple[int, int]] = []    # parallel to tasks: (doc_idx, page0)
    for di, p in enumerate(paths):
        if not p.exists():
            logger.error("Skipping report %s — PDF not found", p)
            continue
        try:
            pdf = fitz.open(p)
        except Exception as exc:  # noqa: BLE001
            logger.error("Skipping report %s — cannot open: %s", p.name, exc)
            continue
        page_natives: list[str] = []
        try:
            for page in pdf:
                native = _extract_native_text(page)
                page_natives.append(native)
                if len(native.strip()) < settings.min_text_chars:
                    tasks.append((str(p), page.number, settings.ocr_dpi))
                    task_owner.append((di, page.number))
        finally:
            pdf.close()
        natives[di] = page_natives
        ok_paths[di] = p

    workers = _resolve_ocr_workers(settings.ocr_max_workers, len(tasks))

    # Phase B: OCR -> results keyed by (doc_idx, page0). ex.map preserves task
    # order, so zipping with task_owner aligns each result to its page.
    results: dict[tuple[int, int], tuple[str, float | None, list[dict]]] = {}
    if tasks and workers > 1:
        logger.info("Parallel OCR: %d scanned pages across %d PDFs on %d workers",
                    len(tasks), len(ok_paths), workers)
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for (_pp, _pno, text, conf, words), owner in zip(
                    ex.map(_ocr_page_task, tasks), task_owner):
                results[owner] = (text, conf, words)
    elif tasks:
        # Serial fallback (1 worker): OCR in-process, reopening each PDF once.
        from collections import defaultdict
        ocr = OCRService()
        by_doc: dict[int, list[int]] = defaultdict(list)
        for di, pno in task_owner:
            by_doc[di].append(pno)
        for di, pnos in by_doc.items():
            pdf = fitz.open(ok_paths[di])
            try:
                for pno in pnos:
                    results[(di, pno)] = _ocr_page(pdf[pno], ocr, settings.ocr_dpi)
            finally:
                pdf.close()

    # Phase C (main process): assemble each IngestedDoc in page order.
    out: list[tuple[Path, IngestedDoc]] = []
    for di in sorted(ok_paths):
        p = ok_paths[di]
        doc = IngestedDoc(file_name=p.name)
        for pi, native in enumerate(natives[di]):
            doc.pages.append(_build_page_text(pi + 1, native, results.get((di, pi))))
        out.append((p, _finalize_doc(doc, p)))
    return out
