"""Conflict detection & resolution (L6) — Phase 2, runtime types only.

Detects insight-vs-insight and restatement conflicts. Does NOT do computed-vs-
stated (the financial core is trusted, architecture §0.3). Internal-vs-external /
cross-api / insight-vs-disclosure require external evidence and activate in Phase 4.

Resolution precedence (architecture §8.2):
  1. workbook financial figure authoritative
  2. newest report year (restatement)
  3. insight recency, then confidence
  4. external reliability, then freshness
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from . import authority, divergence, scale, temporal
from .models import Conflict, EvidenceItem, FactRef

_RESTATE_REL_TOL = 0.005  # 0.5% — ignore rounding noise


_SEM_SYS = (
    "You are given financial insights. Identify pairs that SEMANTICALLY CONTRADICT "
    "each other — assert opposite things about the same subject — even if they are "
    "categorized under different areas or years. Respond JSON: "
    '{"pairs":[{"a_id":str,"b_id":str,"subject":str,"reason":str}]}. '
    "Only genuine contradictions; return an empty list if none. Never invent figures."
)
_SEM_SCHEMA = {
    "type": "object",
    "properties": {"pairs": {"type": "array", "items": {
        "type": "object",
        "properties": {"a_id": {"type": "string"}, "b_id": {"type": "string"},
                       "subject": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["a_id", "b_id"]}}},
    "required": ["pairs"],
}


class ConflictResolver:
    def __init__(self, store, llm=None) -> None:
        self.store = store
        self.llm = llm

    # --- detection ---------------------------------------------------------
    def detect_insight_conflicts(self, resolutions: list[dict]) -> list[Conflict]:
        out: list[Conflict] = []
        for res in resolutions:
            if not res.get("superseded"):
                continue
            w = res["winner"]
            # a 'pick' (clear winner, incl. LLM-adjudicated) is resolved; a 'keep_both'
            # ambiguous tie is left UNRESOLVED so confidence is capped and both surface.
            resolved = res.get("decision", "pick") == "pick"
            out.append(Conflict(
                type="insight_vs_insight",
                topic=res["area"],
                year=w.get("year"),
                values=[{"insight_id": r["insight_id"], "year": r.get("year"),
                         "confidence": r.get("confidence"),
                         "takeaway": (r.get("takeaway") or "")[:120]}
                        for r in [w, *res["superseded"]]],
                resolution=res["rationale"],
                resolved=resolved,
            ))
        return out

    def detect_restatement(self, fact: FactRef, *, preference: str = "latest") -> Optional[Conflict]:
        """A prior-year figure restated across reports: same line, different
        report_year, materially different value in the Source Ledger."""
        sl = self.store.source_ledger
        if sl is None or sl.empty or fact.metric is None:
            return None
        from .ontology import STATEMENT_LINE_TO_DETAIL
        detail_sheet = STATEMENT_LINE_TO_DETAIL.get(fact.metric)
        if detail_sheet is None:
            return None
        rows = sl[(sl.get("Sheet") == detail_sheet) & (sl.get("Year") == fact.year)]
        if rows.empty or "matched_label_norm" not in rows.columns:
            return None

        competing = []
        for label, grp in rows.groupby("matched_label_norm"):
            ry = pd.to_numeric(grp["Report year"], errors="coerce")
            val = pd.to_numeric(grp["Value"], errors="coerce")
            pairs = sorted({(int(y), float(v)) for y, v in zip(ry, val)
                            if pd.notna(y) and pd.notna(v)})
            years = {y for y, _ in pairs}
            if len(years) < 2:
                continue
            vals = [v for _, v in pairs]
            lo, hi = min(vals), max(vals)
            base = max(abs(hi), 1.0)
            if (hi - lo) / base > _RESTATE_REL_TOL:
                competing.append({"line": label, "by_report_year": pairs})

        if not competing:
            return None
        # value_year vs source_report_year: pick the report per the query preference
        # (latest = restated view; as_reported = value as first published).
        all_pairs = [(r, v) for c in competing for r, v in c["by_report_year"]]
        chosen = temporal.prefer(all_pairs, preference)
        label = "newest report" if preference == "latest" else "as first reported"
        return Conflict(
            type="restatement", topic=fact.metric, year=fact.year,
            values=competing[:10],
            resolution=(f"using {label} (FY{chosen[0]}) per "
                        f"{'restatement precedence' if preference == 'latest' else 'as-reported preference'}"),
            resolved=True,
        )

    def detect_semantic_contradictions(self, insights: list[dict], *,
                                       max_insights: int = 12) -> list[Conflict]:
        """Cross-Area semantic contradictions via the LLM (L6a). Catches opposite
        claims that the Area-grouping heuristic misses. Deterministic fallback (no
        LLM / failure): returns [] — same-Area conflicts still handled separately."""
        if self.llm is None or len(insights) < 2:
            return []
        subset = insights[:max_insights]
        by_id = {r["insight_id"]: r for r in subset}
        payload = "\n".join(
            f"[{r['insight_id']}] ({r.get('area')}, FY{r.get('year')}): {r.get('takeaway')}"
            for r in subset)
        data = self.llm.complete_json(_SEM_SYS, payload, _SEM_SCHEMA)
        if not isinstance(data, dict):
            return []
        out: list[Conflict] = []
        seen: set[frozenset] = set()
        for p in data.get("pairs", []):
            a, b = p.get("a_id"), p.get("b_id")
            key = frozenset((a, b))
            if a in by_id and b in by_id and a != b and key not in seen:
                seen.add(key)
                out.append(Conflict(
                    type="insight_vs_insight", topic=p.get("subject") or "cross-area",
                    values=[{"insight_id": i, "area": by_id[i].get("area"),
                             "year": by_id[i].get("year"),
                             "takeaway": (by_id[i].get("takeaway") or "")[:120]}
                            for i in (a, b)],
                    resolution=p.get("reason"), resolved=False,  # exposed, not auto-resolved
                ))
        return out

    def detect_internal_vs_external(self, internal: list[EvidenceItem],
                                    external: list[EvidenceItem]) -> list[Conflict]:
        """Same-metric numeric disagreement between the workbook and an external source.

        Uses ``scale.reconcile`` to distinguish a pure unit mislabel (×10^k —
        "Rupees in thousand" vs "Rs. million") from a genuine value divergence, so a
        scale artifact never surfaces as a conflict. Real divergences are resolved by
        the authority matrix: for an audited financial fact the workbook
        (audited_issuer) dominates external sources (architecture §8.2)."""
        # key by (company, metric) so a peer comparison never matches company A's
        # external value against company B's workbook fact. External items that don't
        # name a company (single-subject sources like overview/quote) fall back to a
        # metric-only match against the first internal fact with that metric.
        by_key: dict[tuple[str | None, str], tuple[float, str, EvidenceItem]] = {}
        for e in internal:
            for f in e.fact_refs:
                if f.metric and f.value is not None:
                    by_key.setdefault((f.company, f.metric), (f.value, f.unit, e))
        out: list[Conflict] = []
        for ext in external:
            loc = ext.citations[0].locator if ext.citations else {}
            metric = loc.get("metric") or loc.get("field")
            if ext.value is None or metric is None:
                continue
            co = loc.get("company")
            if co is not None and (co, metric) in by_key:
                iv, iu, ie = by_key[(co, metric)]
            elif co is None:
                match = next((v for (c, m), v in by_key.items() if m == metric), None)
                if match is None:
                    continue
                iv, iu, ie = match
            else:
                continue                       # external names a company we hold no fact for
            rec = scale.reconcile(iv, iu, ext.value, ext.unit)
            if rec["verdict"] != "divergent":
                continue                       # agree / scale-mislabel / not-comparable -> no conflict
            v = divergence.verdict(ie, ext, claim_type=authority.ClaimType.AUDITED_FACT)
            out.append(Conflict(
                type="internal_vs_external", topic=metric,
                values=[{"source": "workbook", "value": iv, "unit": iu,
                         "canonical": rec["canonical_a"],
                         "authority": authority.authority_class_for(ie).value},
                        {"source": loc.get("source"), "value": ext.value, "unit": ext.unit,
                         "canonical": rec["canonical_b"],
                         "authority": authority.authority_class_for(ext).value}],
                resolution=(f"workbook authoritative for an audited fact; external is "
                            f"{v['chronology'].replace('_', ' ')}"),
                resolved=True))                # trusted baseline settles truth
        return out

    def detect_cross_api(self, external: list[EvidenceItem]) -> list[Conflict]:
        """Same-metric disagreement between TWO external sources (no trusted baseline).
        Scale-reconciled; genuine divergences are SURFACED (resolved=False) with an
        authority-weighted + chronology verdict — never silently picked."""
        # group by (company, metric) so two peers' values for the same metric are
        # never cross-compared (company is None for single-subject external sources).
        groups: dict[tuple[str | None, str], list[EvidenceItem]] = {}
        for e in external:
            loc = e.citations[0].locator if e.citations else {}
            metric = loc.get("metric") or loc.get("field")
            if e.value is not None and metric:
                groups.setdefault((loc.get("company"), metric), []).append(e)
        out: list[Conflict] = []
        for (_co, metric), evs in groups.items():
            for i in range(len(evs)):
                for j in range(i + 1, len(evs)):
                    a, b = evs[i], evs[j]
                    sa = (a.citations[0].locator.get("source") if a.citations else None)
                    sb = (b.citations[0].locator.get("source") if b.citations else None)
                    if sa == sb:
                        continue               # same source -> not cross-api
                    rec = scale.reconcile(a.value, a.unit, b.value, b.unit)
                    if rec["verdict"] != "divergent":
                        continue
                    v = divergence.verdict(a, b)
                    higher = {"side_a_higher_authority": sa, "side_b_higher_authority": sb,
                              "equal_authority": None}[v["authority_weighting"]]
                    note = (f"higher authority: {higher}" if higher else "equal authority")
                    out.append(Conflict(
                        type="cross_api", topic=metric,
                        values=[{"source": sa, "value": a.value, "unit": a.unit,
                                 "authority": authority.authority_class_for(a).value},
                                {"source": sb, "value": b.value, "unit": b.unit,
                                 "authority": authority.authority_class_for(b).value}],
                        resolution=f"{note}; {v['chronology'].replace('_', ' ')} — surfaced for review",
                        resolved=False))       # no trusted baseline -> not resolved
        return out

    def detect(self, *, facts: list[FactRef] | None = None,
               insight_resolutions: list[dict] | None = None,
               insights: list[dict] | None = None,
               report_year_preference: str = "latest") -> list[Conflict]:
        conflicts: list[Conflict] = []
        if insight_resolutions:
            conflicts += self.detect_insight_conflicts(insight_resolutions)
        if insights:
            conflicts += self.detect_semantic_contradictions(insights)
        for f in (facts or []):
            if f.level == "headline":
                c = self.detect_restatement(f, preference=report_year_preference)
                if c:
                    conflicts.append(c)
        return conflicts
