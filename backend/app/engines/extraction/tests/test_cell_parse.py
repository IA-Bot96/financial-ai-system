"""Tests for deterministic cell-parsing hardening (complements GPT, never re-extracts)."""
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.services.cell_parse import normalize_table_values, parse_money


def test_parse_money_accounting_negatives_and_formats():
    assert parse_money("(1,234)") == (-1234.0, True)
    assert parse_money("1,234") == (1234.0, False)
    assert parse_money("−1,234") == (-1234.0, True)   # unicode minus
    assert parse_money("-1234") == (-1234.0, True)
    assert parse_money("Rs 1,234") == (1234.0, False)      # currency word stripped
    assert parse_money("$1,234 CR") == (1234.0, False)     # symbol + trailing CR ignored
    assert parse_money("(0.32)") == (-0.32, True)
    assert parse_money("12.5%") == (12.5, False)


def test_parse_money_empty_and_nil_markers():
    for tok in ("", "-", "–", "—", "n/a", "NA", "nil", "None", "."):
        assert parse_money(tok) == (None, False)
    assert parse_money(None) == (None, False)


def _table(line):
    return FinancialTable(statement_type=StatementType.income_statement,
                          title="Statement of Profit or Loss", line_items=[line])


def test_sign_reconciled_from_raw_when_gpt_dropped_it():
    # GPT returned +1234 but the printed token is "(1,234)" -> flip to negative.
    li = LineItem(label="Finance cost",
                  values=[LineItemValue(year=2025, value=1234.0, raw="(1,234)")])
    counts = normalize_table_values(_table(li))
    assert li.values[0].value == -1234.0 and counts["sign_fixed"] == 1


def test_positive_contra_value_is_not_flipped():
    # Cost printed positive (is_contra) with a positive raw -> left alone.
    li = LineItem(label="Cost of sales",
                  values=[LineItemValue(year=2025, value=500.0, raw="500")])
    counts = normalize_table_values(_table(li))
    assert li.values[0].value == 500.0 and counts["sign_fixed"] == 0


def test_already_negative_value_unchanged():
    li = LineItem(label="Finance cost",
                  values=[LineItemValue(year=2025, value=-1234.0, raw="(1,234)")])
    normalize_table_values(_table(li))
    assert li.values[0].value == -1234.0


def test_note_reference_leak_dropped_when_outlier():
    # note_ref 12; a stray "12" sits among real figures ~6000 -> dropped as a note leak.
    li = LineItem(label="Trade debts", note_ref="12",
                  values=[LineItemValue(year=2024, value=12.0, raw="12"),
                          LineItemValue(year=2025, value=6000.0, raw="6,000")])
    counts = normalize_table_values(_table(li))
    assert li.values[0].value is None and li.values[1].value == 6000.0
    assert counts["note_ref_dropped"] == 1


def test_note_reference_not_dropped_when_plausible_value():
    # note_ref 12 but the line's figures are genuinely small (10, 11) -> 12 kept.
    li = LineItem(label="Some small balance", note_ref="12",
                  values=[LineItemValue(year=2024, value=12.0, raw="12"),
                          LineItemValue(year=2025, value=11.0, raw="11")])
    counts = normalize_table_values(_table(li))
    assert li.values[0].value == 12.0 and counts["note_ref_dropped"] == 0
