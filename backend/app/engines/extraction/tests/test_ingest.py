"""Tests for the rule-based ingest layer (no real PDF / tesseract needed)."""
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.pipeline import ingest


def test_infer_year_prefers_filename():
    assert ingest._infer_year("annual_report_2025.pdf", "cover 2019 2020") == 2025


def test_infer_year_falls_back_to_cover_text():
    assert ingest._infer_year("report.pdf", "Annual Report for the year 2023") == 2023


def test_infer_year_ignores_implausible_numbers():
    assert ingest._infer_year("doc_12345.pdf", "page 1801 of figures") is None


def test_infer_year_picks_largest_plausible():
    assert ingest._infer_year("compare 2021 vs 2022 vs 2023.pdf", "") == 2023


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


def test_full_text_concatenates_pages():
    doc = IngestedDoc(file_name="x.pdf")
    doc.pages = [
        PageText(page=1, text="hello", char_count=5),
        PageText(page=2, text="world", char_count=5),
    ]
    assert doc.full_text == "hello\nworld"
