"""Contract-integrity boot check: the engine's internal wiring contracts hold,
and a deliberate mis-wire is caught (fail-fast) rather than silently served."""

import ast

import pytest

from app.engines.fie import bootcheck
from app.engines.fie.bootcheck import ContractError, CheckResult
from app.engines.fie.calc import registry as formula_registry


def test_all_contracts_pass_on_real_config():
    results = bootcheck.verify_contracts()
    failed = [r for r in results if not r.ok]
    assert not failed, f"unexpected contract failures: {failed}"
    # assert_contracts returns the results and does not raise
    assert bootcheck.assert_contracts() == results


def test_every_check_returns_a_named_result():
    names = {r.name for r in bootcheck.verify_contracts()}
    assert {"authority_matrix", "formula_expressions",
            "understanding_formulas_registered", "formula_inputs_canonical",
            "citation_precision_order", "taxonomy"} <= names


# --- negative tests: a mis-wire must be caught -----------------------------
def test_undeclared_formula_identifier_is_caught(monkeypatch):
    """A formula whose expression references an undeclared input fails the check."""
    bad = formula_registry.FormulaSpec(
        id="_bad_test", category="growth", expression="rev_t / ghost_input",
        inputs=[formula_registry._i("rev_t", "revenue")])
    patched = dict(formula_registry.REGISTRY)
    patched["_bad_test"] = bad
    monkeypatch.setattr(formula_registry, "REGISTRY", patched)
    res = bootcheck._check_formula_expressions()
    assert res.ok is False and "ghost_input" in res.detail
    with pytest.raises(ContractError):
        bootcheck.assert_contracts()


def test_phantom_metric_input_is_caught(monkeypatch):
    bad = formula_registry.FormulaSpec(
        id="_bad_metric", category="growth", expression="x",
        inputs=[formula_registry._i("x", "not_a_real_metric")])
    patched = dict(formula_registry.REGISTRY)
    patched["_bad_metric"] = bad
    monkeypatch.setattr(formula_registry, "REGISTRY", patched)
    res = bootcheck._check_formula_inputs_canonical()
    assert res.ok is False and "not_a_real_metric" in res.detail


def test_scrambled_citation_precision_is_caught(monkeypatch):
    from app.engines.fie import citation_enforce
    monkeypatch.setattr(citation_enforce, "_RANK",
                        {"CELL": 1, "PAGE": 2, "REF": 3, "NONE": 4})
    res = bootcheck._check_citation_precision_order()
    assert res.ok is False
