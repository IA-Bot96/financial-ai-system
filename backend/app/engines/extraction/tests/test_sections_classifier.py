"""Tests for section detection and the local classifier (keyword-only mode)."""
import pytest

from app.core.config import get_settings
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.pipeline.sections import detect_sections, section_for_page
from app.engines.extraction.services.classifier import TableClassifier


def _doc() -> IngestedDoc:
    doc = IngestedDoc(file_name="report.pdf", report_year=2025)
    doc.pages = [
        PageText(page=1, text="Cover\nAnnual Report 2025", kind=PageKind.native, char_count=20),
        PageText(page=2, text="Statement of Financial Position\nTotal assets ...", kind=PageKind.native, char_count=40),
        PageText(page=3, text="(continued)\nMore balance sheet rows", kind=PageKind.native, char_count=30),
        PageText(page=4, text="Statement of Profit or Loss\nOperating profit", kind=PageKind.native, char_count=40),
    ]
    doc.page_count = 4
    return doc


def test_detect_sections_ranges():
    sections = detect_sections(_doc())
    assert [s.statement_type for s in sections] == [
        StatementType.balance_sheet,
        StatementType.income_statement,
    ]
    bs = sections[0]
    assert (bs.start_page, bs.end_page) == (2, 3)  # extends until the next heading


def test_section_for_page():
    sections = detect_sections(_doc())
    assert section_for_page(sections, 3).statement_type == StatementType.balance_sheet
    assert section_for_page(sections, 1) is None


def test_consolidated_qualifier_detection():
    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [
        PageText(page=1, text="Unconsolidated Statement of Financial Position\nTotal assets", kind=PageKind.native, char_count=40),
        PageText(page=2, text="Consolidated Statement of Profit or Loss\nRevenue", kind=PageKind.native, char_count=40),
    ]
    doc.page_count = 2
    sections = detect_sections(doc)
    assert sections[0].statement_type == StatementType.balance_sheet
    assert sections[0].consolidated is False   # unconsolidated
    assert sections[1].statement_type == StatementType.income_statement
    assert sections[1].consolidated is True    # consolidated


@pytest.fixture
def keyword_only_classifier(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("USE_EMBEDDINGS", "false")
    clf = TableClassifier()
    yield clf
    get_settings.cache_clear()


def test_classify_balance_sheet(keyword_only_classifier):
    result = keyword_only_classifier.classify("Statement of Financial Position total assets total equity")
    assert result.statement_type == StatementType.balance_sheet
    assert result.needs_review is False


def test_classify_income_statement(keyword_only_classifier):
    result = keyword_only_classifier.classify(
        "Statement of Profit or Loss operating profit profit after tax for the year earnings per share"
    )
    assert result.statement_type == StatementType.income_statement
    assert result.needs_review is False


def test_classify_ambiguous_flags_review(keyword_only_classifier):
    result = keyword_only_classifier.classify("Our products serve many regional markets")
    assert result.statement_type == StatementType.unclassified
    assert result.needs_review is True


def test_section_hint_breaks_tie(keyword_only_classifier):
    # Weak text, but the section hint should push it over the line.
    result = keyword_only_classifier.classify("table", section_hint=StatementType.income_statement)
    assert result.scores[StatementType.income_statement] > 0


def test_classify_note_level_cost_of_sales(keyword_only_classifier):
    sig = "Cost of Sales manufacturing cost raw material consumed work-in-process"
    assert keyword_only_classifier.classify(sig).statement_type == StatementType.cost_of_sales


def test_classify_note_level_revenue(keyword_only_classifier):
    sig = "Gross Revenue local sales export sales net revenue"
    assert keyword_only_classifier.classify(sig).statement_type == StatementType.revenue


def test_classify_note_level_current_liabilities(keyword_only_classifier):
    sig = "Trade and other payables trade creditors short-term borrowings"
    assert keyword_only_classifier.classify(sig).statement_type == StatementType.current_liabilities
