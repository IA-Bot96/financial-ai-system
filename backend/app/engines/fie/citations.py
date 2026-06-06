"""Citation binding (L8a) — Phase 1.

Gathers citations already resolved during retrieval/calc, dedupes and renumbers
them with stable inline handles, and reports any FactRef that could not be cited
(provenance_basis == "none") so the caller can withhold it.

Architecture §7.2: no number is rendered without a resolvable citation.
"""

from __future__ import annotations

from .models import CalcResult, Citation, EvidenceItem, FactRef


def _locator_key(c: Citation) -> tuple:
    loc = c.locator or {}
    # external (news) items have no sheet/cell/page — identify them by article
    # link/source so distinct articles aren't collapsed into one citation, while
    # multiple chunks of the SAME article (same link) still merge to one cite.
    return (loc.get("report_file"), loc.get("page"), loc.get("table_id"),
            loc.get("sheet"), loc.get("cell"), loc.get("year"),
            loc.get("link") or loc.get("url"), loc.get("source"), loc.get("insight_id"),
            # external datasets (e.g. analysis_reports) serve many distinct facts from
            # one source — keep per-(symbol, metric) facts as distinct citations.
            loc.get("symbol"), loc.get("metric") or loc.get("field"))


def bind(evidence: list[EvidenceItem], calcs: list[CalcResult]
         ) -> tuple[list[Citation], list[FactRef]]:
    """Return (deduped+renumbered citations, withheld facts)."""
    seen: dict[tuple, Citation] = {}
    ordered: list[Citation] = []
    withheld: list[FactRef] = []

    def _collect(cites: list[Citation]):
        for c in cites:
            key = _locator_key(c)
            if key not in seen:
                seen[key] = c
                ordered.append(c)

    for ev in evidence:
        _collect(ev.citations)
        for f in ev.fact_refs:
            if f.provenance_basis == "none" and f.value is not None:
                withheld.append(f)
    for cr in calcs:
        _collect(cr.citations)

    # renumber with stable inline handles C1..Cn
    for i, c in enumerate(ordered, start=1):
        c.ref_id = f"C{i}"
    # propagate the canonical handle to de-duped citation objects so EVERY evidence/
    # calc citation resolves to a real Cn (not a stale 'C?') — required for claim-level
    # citation enforcement to map a finding's handle back to its citation.
    ref_by_key = {_locator_key(c): c.ref_id for c in ordered}
    for ev in evidence:
        for c in ev.citations:
            c.ref_id = ref_by_key.get(_locator_key(c), c.ref_id)
    for cr in calcs:
        for c in cr.citations:
            c.ref_id = ref_by_key.get(_locator_key(c), c.ref_id)
    return ordered, withheld
