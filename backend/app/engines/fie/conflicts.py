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

from .models import Conflict, FactRef

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

    def detect_restatement(self, fact: FactRef) -> Optional[Conflict]:
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
        newest = max(r for c in competing for r, _ in c["by_report_year"])
        return Conflict(
            type="restatement", topic=fact.metric, year=fact.year,
            values=competing[:10],
            resolution=f"using newest report (FY{newest}) per restatement precedence",
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

    def detect(self, *, facts: list[FactRef] | None = None,
               insight_resolutions: list[dict] | None = None,
               insights: list[dict] | None = None) -> list[Conflict]:
        conflicts: list[Conflict] = []
        if insight_resolutions:
            conflicts += self.detect_insight_conflicts(insight_resolutions)
        if insights:
            conflicts += self.detect_semantic_contradictions(insights)
        for f in (facts or []):
            if f.level == "headline":
                c = self.detect_restatement(f)
                if c:
                    conflicts.append(c)
        return conflicts
