"""Tests for candidate-page selection — the consolidated-skip guardrail that must
never drop a standalone primary statement (the Millat-2022 root cause)."""
from app.engines.extraction.models.document import IngestedDoc, PageText
from app.engines.extraction.pipeline.gpt_tables import _candidate_pages, _looks_primary_statement

# Enough comma-grouped figures + a signal word so _is_financial_page accepts the page.
_NUMS = " ".join(f"{i},{i:03d},{i:03d}" for i in range(1, 10))
_FIN = f"revenue cost of sales {_NUMS}"


def _doc(pages):
    return IngestedDoc(file_name="r.pdf", page_count=len(pages),
                       pages=[PageText(page=p, text=t) for p, t in pages])


def test_primary_statement_signal_detection():
    assert _looks_primary_statement("... Total assets 1,234,567 ...")
    assert _looks_primary_statement("Profit for the year 9,999")
    assert _looks_primary_statement("TOTAL EQUITY AND LIABILITIES 5,000")
    assert not _looks_primary_statement("Trade and other payables note 1,234")


def test_consolidated_flagged_primary_statement_is_not_skipped():
    # p10 is consolidated-flagged AND carries a primary total -> must be kept;
    # p11 is consolidated-flagged plain note -> skipped on a template run.
    doc = _doc([(10, _FIN + " Total assets 12,345,678"), (11, _FIN + " some note rows")])
    ctx = {10: True, 11: True}
    kept = {p.page for p in _candidate_pages(doc, ctx, 1, True, 6, 120)}
    assert 10 in kept and 11 not in kept


def test_no_skip_when_not_template_run():
    doc = _doc([(10, _FIN), (11, _FIN)])
    ctx = {10: True, 11: True}
    kept = {p.page for p in _candidate_pages(doc, ctx, 1, False, 6, 120)}
    assert kept == {10, 11}                       # skip_consolidated=False keeps all


def test_unconsolidated_pages_always_kept():
    doc = _doc([(10, _FIN), (11, _FIN)])
    ctx = {10: False, 11: None}
    kept = {p.page for p in _candidate_pages(doc, ctx, 1, True, 6, 120)}
    assert kept == {10, 11}
