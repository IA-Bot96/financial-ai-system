"""Tests for the rule-based ingest layer (no real PDF / tesseract needed)."""
from pathlib import Path

from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.pipeline import ingest


def test_infer_year_prefers_document_over_filename():
    # Document is the source of truth even when the filename has a different year.
    assert ingest._infer_year("annual_report_2020.pdf", "Annual Report 2025") == 2025


def test_infer_year_falls_back_to_filename():
    # No year in the document -> use the filename.
    assert ingest._infer_year("compare_2023.pdf", "no years here") == 2023


def test_infer_year_ignores_implausible_numbers():
    assert ingest._infer_year("doc_12345.pdf", "page 1801 of figures") is None


def test_infer_company_from_document_recurring_header():
    pages = [
        "Annual Report 2025\nThe board of Millat Tractors Limited is pleased...",
        "Millat Tractors Limited\nStatement of Financial Position",
        "Millat Tractors Limited\nStatement of Profit or Loss",
    ]
    assert ingest._infer_company(pages, "millat-2025.pdf") == "Millat Tractors Limited"


def test_infer_company_overrides_filename():
    # Even with a clean filename, the in-document name wins.
    pages = ["Lucky Cement Limited\nConsolidated Financial Statements"]
    assert ingest._infer_company(pages, "report_xyz.pdf") == "Lucky Cement Limited"


def test_infer_company_falls_back_to_filename_when_absent():
    assert ingest._infer_company(["no company name here"], "millat-2025.pdf") == "Millat"


def test_is_scanned_majority_rule():
    doc = IngestedDoc(file_name="x.pdf")
    doc.pages = [
        PageText(page=1, text="a", kind=PageKind.ocr, char_count=1),
        PageText(page=2, text="b", kind=PageKind.ocr, char_count=1),
        PageText(page=3, text="c", kind=PageKind.native, char_count=1),
    ]
    doc.page_count = 3
    # Recompute the way ingest does.
    doc.is_scanned = doc.page_count > 0 and doc.ocr_page_count > doc.page_count / 2
    assert doc.ocr_page_count == 2
    assert doc.is_scanned is True


def test_ocr_words_excluded_from_serialization():
    page = PageText(page=1, text="x", char_count=1,
                    ocr_words=[{"text": "x", "x0": 0, "x1": 1, "top": 0, "bottom": 1}])
    assert page.ocr_words  # available in memory for Layer 2
    assert "ocr_words" not in page.model_dump()  # not serialized into output


def test_full_text_concatenates_pages():
    doc = IngestedDoc(file_name="x.pdf")
    doc.pages = [
        PageText(page=1, text="hello", char_count=5),
        PageText(page=2, text="world", char_count=5),
    ]
    assert doc.full_text == "hello\nworld"


# --- Parallel ingest (flat OCR pool) -----------------------------------------

def _make_pdf(tmp_path, name, page_lines):
    """Write a tiny native-text PDF (no tesseract needed)."""
    import fitz

    doc = fitz.open()
    for text in page_lines:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    p = Path(tmp_path) / name
    doc.save(p)
    doc.close()
    return p


def test_build_page_text_native():
    pt = ingest._build_page_text(1, "native text here", None)
    assert pt.kind == PageKind.native and pt.text == "native text here" and pt.ocr_words == []


def test_build_page_text_ocr_wins_when_longer():
    pt = ingest._build_page_text(2, "x", ("a much longer ocr result", 88.0, [{"text": "a"}]))
    assert pt.kind == PageKind.ocr and pt.ocr_confidence == 88.0 and pt.ocr_words


def test_build_page_text_ocr_loses_to_native():
    pt = ingest._build_page_text(3, "long native baseline text", ("hi", 50.0, [{"text": "a"}]))
    assert pt.kind == PageKind.empty and pt.text == "long native baseline text" and pt.ocr_words == []


def test_resolve_ocr_workers():
    assert ingest._resolve_ocr_workers(0, 0) == 1          # nothing to OCR -> serial
    assert ingest._resolve_ocr_workers(0, 1) == 1          # 1 page -> serial
    assert ingest._resolve_ocr_workers(4, 10) == 4         # honour explicit cap
    assert ingest._resolve_ocr_workers(4, 2) == 2          # never exceed task count
    assert ingest._resolve_ocr_workers(0, 100) >= 1        # auto = cpu-1, at least 1


def test_resolve_ocr_workers_forces_serial_when_frozen(monkeypatch):
    # A PyInstaller-frozen (packaged) build can't spawn pool workers -> always serial,
    # regardless of configured workers or task count. Prevents BrokenProcessPool.
    import sys
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert ingest._resolve_ocr_workers(8, 100) == 1
    assert ingest._resolve_ocr_workers(0, 100) == 1


def test_ingest_pdfs_matches_serial_for_native(tmp_path):
    # Parallel batch path must produce the same doc as the serial single-PDF path.
    p = _make_pdf(tmp_path, "millat-2025.pdf", [
        "Millat Tractors Limited Annual Report 2025 Statement of Financial Position alpha beta",
        "Millat Tractors Limited Statement of Profit or Loss gamma delta epsilon zeta eta",
    ])
    serial = ingest.ingest_pdf(p)
    batch = ingest.ingest_pdfs([p])
    assert len(batch) == 1
    rp, doc = batch[0]
    assert rp == p
    assert [pg.text for pg in doc.pages] == [pg.text for pg in serial.pages]
    assert [pg.kind for pg in doc.pages] == [pg.kind for pg in serial.pages]
    assert doc.company == serial.company == "Millat Tractors Limited"
    assert doc.report_year == serial.report_year == 2025
    assert doc.page_count == serial.page_count == 2


def test_ingest_pdfs_assembles_ocr_pages_in_order(tmp_path, monkeypatch):
    # A near-empty page is classified scanned -> OCR task. Force the serial pool
    # branch and stub OCR so no tesseract/process is needed; verify the OCR text
    # lands on the right page with kind=ocr.
    p = _make_pdf(tmp_path, "scan-2024.pdf", ["hi", "y"])  # both < min_text_chars
    monkeypatch.setattr(ingest, "_resolve_ocr_workers", lambda configured, n: 1)
    monkeypatch.setattr(ingest, "_ocr_page", lambda page, ocr, dpi:
                        (f"recovered ocr page {page.number + 1}", 91.0,
                         [{"text": "w", "x0": 0, "x1": 1, "top": 0, "bottom": 1}]))
    _, doc = ingest.ingest_pdfs([p])[0]
    assert [pg.kind for pg in doc.pages] == [PageKind.ocr, PageKind.ocr]
    assert doc.pages[0].text == "recovered ocr page 1"
    assert doc.pages[1].text == "recovered ocr page 2"
    assert all(pg.ocr_confidence == 91.0 and pg.ocr_words for pg in doc.pages)


def test_ingest_pdfs_skips_unopenable_pdf(tmp_path):
    # A missing PDF is logged and skipped; the rest of the batch still ingests.
    good = _make_pdf(tmp_path, "good-2025.pdf", ["Lucky Cement Limited 2025 report body text here"])
    missing = Path(tmp_path) / "nope.pdf"
    out = ingest.ingest_pdfs([missing, good])
    assert [rp for rp, _ in out] == [good]
