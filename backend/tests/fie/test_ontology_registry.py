"""Ontology consolidation onto the shared canonical_metric_registry.json.

The FIE ontology is registry-backed (single source of truth for label->canonical),
reconciles divergent registry ids to FIE ids, and keeps FIE seed aliases as
higher-precedence overrides so existing mappings never change.
"""

from app.engines.fie.ontology import REGISTRY_TO_FIE, MetricOntology


def test_registry_loaded_expands_coverage():
    seed_only = MetricOntology(use_registry=False)
    full = MetricOntology()  # registry-backed
    # the registry contributes far more aliases than the seed alone
    assert len(full._alias_keys) > len(seed_only._alias_keys) * 5


def test_registry_aliases_reconcile_to_fie_ids():
    o = MetricOntology()
    # registry canonical 'profit_after_tax' is reconciled to FIE id 'pat'
    assert o.canonical("profit for the year") == "pat"
    assert o.canonical("profit after tax for the year") == "pat"
    # registry 'equity' / 'cash_and_bank_balances' reconcile to FIE ids
    assert o.canonical("turnover") == "revenue"


def test_fie_seed_overrides_registry():
    o = MetricOntology()
    # the exact workbook labels the FIE depends on still map to FIE ids
    assert o.canonical("Revenue from contracts with customers") == "revenue"
    assert o.canonical("Cash and bank balances") == "cash_and_bank"


def test_reconcile_targets_are_fie_ids():
    # every reconciled target must be a real FIE canonical id used by formulas
    fie_ids = {"pat", "total_equity", "cash_and_bank", "share_capital_reserves"}
    assert set(REGISTRY_TO_FIE.values()) <= fie_ids


def test_missing_registry_falls_back_to_seed():
    o = MetricOntology(registry_path="/nonexistent/path.json")
    assert o.canonical("revenue") == "revenue"  # seed still works


def test_no_mapping_regression_on_known_metrics(millat_store):
    # the headline metrics still resolve to the same authoritative cells/values
    assert millat_store.lookup("revenue", 2024).value == 91_534_501.0
    assert millat_store.lookup("pat", 2024).cell == "F24"
    assert millat_store.lookup("total_equity", 2024).value == 11_628_983.0
