"""Tests for the rule-based ingest layer (no real PDF / tesseract needed)."""
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
