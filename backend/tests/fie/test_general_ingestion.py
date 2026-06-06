"""General (structure-agnostic) ingestion + cash-flow/valuation formulas + Macro.

Uses the no-template OCR workbook (each statement is its own title-named sheet,
no Source Ledger, includes a cash-flow statement) to prove the engine is not tied
to the templated structure.
"""

import os

import pytest

from app.engines.fie import FinancialFactStore, Macro
from app.engines.fie.apis import ApiClient
from app.engines.fie.calc import CalcEngine
from app.engines.fie.ingest.classify import classify_sheet, statement_family


@pytest.fixture(scope="module")
def notmpl_store(outputs_dir):
    path = os.path.join(outputs_dir, "millat_no_template_v3.xlsx")
    return FinancialFactStore.from_workbook(path)


# --- general classification (title-based, not exact names) ---

@pytest.mark.parametrize("title,fam", [
    ("Unconsolidated Statement of Pro", "pl"),
    ("Unconsolidated Statement of Fin", "bs"),
    ("Unconsolidated Statement of Cas", "cf"),
    ("Unconsolidated Statement of Cha", "equity"),
    ("P&L", "pl"),
    ("Balance Sheet", "bs"),
    ("Cost of sales (Unconsolidated)", None),  # a note -> generic detail
])
def test_statement_family_inference(title, fam):
    assert statement_family(title) == fam


def test_notes_classified_as_detail():
    assert classify_sheet("Revenue from contracts with cus") == "detail"
    assert classify_sheet("Unconsolidated Statement of Cas") == "statement"


# --- general ingestion of the no-template workbook ---

def test_notemplate_loads_and_resolves(notmpl_store):
    assert notmpl_store.years[:3] == [2021, 2022, 2023]
    assert notmpl_store.lookup("revenue", 2024).value == 91_534_501.0
    assert notmpl_store.lookup("cost_of_sales", 2025).value == -38_241_906.0


def test_cashflow_statement_ingested(notmpl_store):
    # cash flow values present and tagged statement='cf'
    cfo = notmpl_store.lookup("cash_generated_from_operations", 2025)
    assert cfo.value == 10_253_249.0 and cfo.statement == "cf"
    assert notmpl_store.lookup("operating_cash_flow", 2025).value == 3_341_999.0


def test_workbook_cell_citation_fallback(notmpl_store):
    f = notmpl_store.lookup("operating_cash_flow", 2025)
    cites = notmpl_store.cite(f)
    assert cites and cites[0].locator["basis"] == "workbook"
    assert f.provenance_basis == "workbook"


# --- cash-flow formula computes on the no-template workbook ---

def test_free_cash_flow(notmpl_store):
    r = CalcEngine(notmpl_store).evaluate("free_cash_flow", 2025)
    # OCF 3,341,999 + capex (-439,884)
    assert r.value == 3_341_999.0 + (-439_884.0)
    assert r.confidence in ("High", "Medium")


def test_valuation_blocks_report_missing_when_data_absent(notmpl_store):
    # this workbook lacks clean equity/shares -> formulas honestly report missing
    r = CalcEngine(notmpl_store).evaluate("book_value_per_share", 2025)
    assert r.value is None and "missing inputs" in r.note


# --- new formulas exist in the registry ---

@pytest.mark.parametrize("fid", ["free_cash_flow", "ebitda", "book_value_per_share",
                                 "eps_computed", "cash_ratio"])
def test_registry_has_formula(fid):
    from app.engines.fie.calc import registry
    assert registry.get(fid) is not None


# --- Macro adapter (offline) ---

class _FakeT:
    def get(self, url, params, timeout):
        return {"indicators": {"policy_rate": 11.0, "inflation": 4.9, "fx_usd_pkr": 278.5}}
    def post(self, url, body, timeout):
        raise AssertionError("macro is GET")


def test_macro_adapter_normalizes():
    m = Macro(ApiClient(_FakeT(), sleep=lambda s: None, now=lambda: "2026-06-06"))
    res = m.indicators("PK")
    assert res.status == "ok"
    inds = {i.citations[0].locator["indicator"]: i.value for i in res.items}
    assert inds["policy_rate"] == 11.0 and inds["fx_usd_pkr"] == 278.5
    assert all(i.kind == "external" for i in res.items)
