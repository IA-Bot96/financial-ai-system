"""Invariants + glossary-alias coverage for the canonical metric registry."""
import json
from pathlib import Path

from app.engines.extraction.services.metric_resolver import get_resolver

_REGISTRY = Path(__file__).resolve().parents[1] / "data" / "canonical_metric_registry.json"


def test_no_alias_maps_to_multiple_canonicals():
    reg = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    owner: dict[str, list[str]] = {}
    for cm, meta in reg.items():
        for a in meta.get("aliases", []):
            owner.setdefault(a.strip().lower(), []).append(cm)
    collisions = {a: cs for a, cs in owner.items() if len(set(cs)) > 1}
    assert not collisions, f"alias collisions: {collisions}"


def test_glossary_aliases_resolve():
    r = get_resolver()
    cases = {
        "Debtors": "trade_receivables", "PBT": "profit_before_tax", "COGS": "cost_of_goods_sold",
        "Tangible assets": "property_plant_equipment", "Accumulated profits": "retained_earnings",
        "Securities premium": "share_premium", "Treasury stock": "treasury_shares",
        "Net cash used in investing activities": "investing_cash_flow",
        "Bank balances and cash": "cash_and_cash_equivalents", "OPEX": "operating_expenses",
        # Equity-total captions (regression: "Total share capital and reserves" was unresolved).
        "Total share capital and reserves": "share_capital_and_reserves",
        "Total shareholders equity": "equity",
    }
    for label, expected in cases.items():
        m = r.resolve(label)
        assert m is not None and m.canonical_key == expected, f"{label!r} -> {m and m.canonical_key} (want {expected})"
