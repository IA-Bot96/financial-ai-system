"""Internal retrieval (L3a) — Phase 1.

Executes a SourcePlan's internal requirements against the FinancialFactStore,
producing EvidenceItems that carry FactRefs and resolved Citations. No external
APIs. See docs/fie_implementation_plan.md §Phase 1 (1.4).
"""

from __future__ import annotations

from .models import EvidenceItem, SourcePlan
from .store import FinancialFactStore


def fetch(store: FinancialFactStore, plan: SourcePlan) -> list[EvidenceItem]:
    """Resolve each internal requirement to an EvidenceItem (value + provenance)."""
    items: list[EvidenceItem] = []
    for req in plan.requirements:
        if req.kind != "internal":
            continue  # external handled in Phase 4
        try:
            fact = store.lookup(req.metric, req.year, level=req.level,
                                period_type=req.period_type)
        except KeyError:
            items.append(EvidenceItem(
                claim=f"{req.metric} {req.year} not found",
                value=None, kind="statement",
            ))
            continue
        cites = store.cite(fact)
        items.append(EvidenceItem(
            claim=f"{req.metric} {req.year} = {fact.value}",
            value=fact.value, unit=fact.unit, kind="statement",
            fact_refs=[fact], citations=cites,
        ))
    return items


def by_metric(items: list[EvidenceItem]) -> dict[str, EvidenceItem]:
    """Index evidence by the canonical metric of its first FactRef."""
    out: dict[str, EvidenceItem] = {}
    for it in items:
        if it.fact_refs and it.fact_refs[0].metric:
            out[it.fact_refs[0].metric] = it
    return out


def evidence_from_facts(store: FinancialFactStore, facts) -> list[EvidenceItem]:
    """Wrap already-resolved FactRefs (e.g. calc inputs) as cited EvidenceItems."""
    items: list[EvidenceItem] = []
    for f in facts:
        items.append(EvidenceItem(
            claim=f"{f.metric} {f.year} = {f.value}",
            value=f.value, unit=f.unit, kind=("detail" if f.level == "detail" else "statement"),
            fact_refs=[f], citations=store.cite(f),
        ))
    return items
