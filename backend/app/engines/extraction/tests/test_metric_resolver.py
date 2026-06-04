"""Tests for the registry-backed metric resolver."""
import pytest

from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.table import RawTable
from app.engines.extraction.pipeline.structure import build_financial_table
from app.engines.extraction.services.metric_resolver import MetricResolver, squash


@pytest.fixture(scope="module")
def resolver():
    return MetricResolver(use_embeddings=False)


def test_squash_absorbs_space_garble():
    assert squash("Cash and cash equivalen ts") == "cashandcashequivalents"
    assert squash("Operating  Profit!") == "operatingprofit"


def test_exact_synonym(resolver):
    m = resolver.resolve("Turnover")
    assert m is not None and m.canonical_key == "revenue" and m.method == "exact"


def test_space_garble_resolves_via_normalization(resolver):
    m = resolver.resolve("Cash and cash equivalen ts")
    assert m is not None and m.canonical_key == "cash_and_cash_equivalents"


def test_fuzzy_handles_char_corruption(resolver):
    m = resolver.resolve("Operatng Profit")  # dropped 'i'
    assert m is not None and m.canonical_key == "operating_profit"


def test_revenue_reserves_not_mismapped_to_revenue(resolver):
    # The dangerous substring case: must NOT collapse to `revenue`.
    m = resolver.resolve("Revenue reserves")
    assert m is not None and m.canonical_key == "revenue_reserves"
    assert m.category == "balance_sheet"


def test_unknown_leaf_label_returns_none(resolver):
    assert resolver.resolve("Multi-application products service income widget") is None


def test_build_financial_table_tags_canonical_metric():
    raw = RawTable(
        table_id="t0", statement_type=StatementType.income_statement, title="IS",
        header=["", "2024", "2025"], rows=[["Turnover", "100", "120"]], years=[2024, 2025],
    )
    ft = build_financial_table(raw)
    li = ft.line_items[0]
    assert li.canonical_metric == "revenue" and li.canonical_category == "income_statement"
