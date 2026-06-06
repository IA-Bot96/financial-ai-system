"""Tests for reporting-basis detection from statement titles (glossary synonyms)."""
from types import SimpleNamespace

from app.engines.extraction.services.face_truth import _table_basis


def _b(title):
    return _table_basis(SimpleNamespace(title=title))


def test_explicit_basis():
    assert _b("Unconsolidated Statement of Financial Position") == "unconsolidated"
    assert _b("Consolidated Statement of Profit or Loss") == "consolidated"
    assert _b("Statement of Cash Flows") == "unknown"


def test_glossary_unconsolidated_synonyms():
    assert _b("Separate Statement of Financial Position") == "unconsolidated"
    assert _b("Standalone Statement of Profit or Loss") == "unconsolidated"
    assert _b("Stand-alone Cash Flow Statement") == "unconsolidated"
    assert _b("Parent Company Statement of Financial Position") == "unconsolidated"
    assert _b("Company Financial Statements") == "unconsolidated"


def test_glossary_consolidated_synonyms():
    assert _b("Group Financial Statements") == "consolidated"
    assert _b("Group Accounts") == "consolidated"


def test_unconsolidated_wins_over_consolidated_substring():
    # "unconsolidated" contains "consolidated" — must classify as unconsolidated.
    assert _b("Unconsolidated Statement of Cash Flows") == "unconsolidated"


def test_bare_group_or_company_not_a_basis_signal():
    # Far too common to be a basis signal on their own.
    assert _b("Millat Tractors Company Statement of Profit or Loss") == "unknown"
    assert _b("Engro Group Statement of Financial Position") == "unknown"
    assert _b("Notes to the Company's Accounts") == "unknown"
