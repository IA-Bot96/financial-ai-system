"""Insight selection & ranking (part of L3/L6) — Phase 2.

Insights are selected by relevance to the query, then deconflicted. Insight-vs-
insight conflicts resolve by **year, then confidence** (default), with a
configurable blended-score mode. Superseded insights are retained as caveats,
not discarded.

Deterministic-core scope: "same topic" is keyed by the insight ``Area``; semantic
contradiction detection within an Area is layered on by the LLM in Phase 3. This
module owns the ranking/resolution mechanism.

See architecture §3.4 and docs/fie_implementation_plan.md §Phase 2 (2.3).
"""

from __future__ import annotations

from typing import Literal, Optional

from rapidfuzz import fuzz

from .models import Citation, EvidenceItem, QueryFrame

ResolveMode = Literal["year_then_confidence", "blended"]


def insight_evidence(rec: dict, ref_id: str = "C?") -> EvidenceItem:
    """Wrap an insight record as a cited EvidenceItem (kind='insight')."""
    parts = [rec.get("source_section"), f"p{rec['page']}" if rec.get("page") else None,
             f"FY{rec['year']}" if rec.get("year") else None]
    display = " ".join(p for p in parts if p) or "workbook insight"
    cite = Citation(
        ref_id=ref_id, kind="insight", display=display,
        locator={"insight_id": rec.get("insight_id"), "area": rec.get("area"),
                 "source_section": rec.get("source_section"), "page": rec.get("page"),
                 "year": rec.get("year")},
        confidence=rec.get("confidence"),
    )
    return EvidenceItem(
        claim=rec.get("takeaway") or "", kind="insight",
        reliability=(rec.get("confidence") or 0.0), citations=[cite],
    )


def _relevance(rec: dict, frame: QueryFrame) -> float:
    """0..1 relevance of an insight to the query (rule-based)."""
    text = " ".join(filter(None, [rec.get("area"), rec.get("takeaway")])).lower()
    if not text:
        return 0.0

    terms: list[str] = []
    terms += [m.replace("_", " ") for m in frame.metrics]
    if frame.intent == "risk_assessment":
        terms += ["risk", "margin", "pressure", "liquidity", "demand", "cost"]
    # words from the raw query (drop short stopwords)
    terms += [w for w in frame.raw_query.lower().split() if len(w) > 3]

    topical = max((fuzz.partial_ratio(t, text) for t in terms), default=0) / 100.0

    # temporal affinity: exact year match boosts; else mild recency preference
    temporal = 1.0
    if frame.year is not None and rec.get("year") is not None:
        temporal = 1.0 if rec["year"] == frame.year else 0.6

    return round(0.7 * topical + 0.3 * temporal, 4)


def _rank_value(rec: dict, mode: ResolveMode, alpha: float, yr_range: tuple[int, int]):
    year = rec.get("year") or 0
    conf = rec.get("confidence") or 0.0
    if mode == "year_then_confidence":
        return (year, conf)  # lexicographic: newer wins, confidence breaks ties
    ymin, ymax = yr_range
    rnorm = (year - ymin) / (ymax - ymin) if ymax > ymin else 1.0
    return alpha * rnorm + (1.0 - alpha) * conf


_ADJ_SYS = (
    "You adjudicate two financial insights about the same topic and year. Decide if "
    "they actually CONTRADICT. Respond JSON: {contradict: bool, keep_both: bool, "
    "winner_id: string|null, reason: string}. Prefer keep_both unless one is clearly "
    "more credible. Never invent figures."
)
_ADJ_SCHEMA = {
    "type": "object",
    "properties": {
        "contradict": {"type": "boolean"},
        "keep_both": {"type": "boolean"},
        "winner_id": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["contradict"],
}


def _adj_user(a: dict, b: dict) -> str:
    return (f"Topic/Area: {a.get('area')}; Year: {a.get('year')}\n"
            f"[{a['insight_id']}] (conf {a.get('confidence')}): {a.get('takeaway')}\n"
            f"[{b['insight_id']}] (conf {b.get('confidence')}): {b.get('takeaway')}")


class InsightSelector:
    def __init__(self, mode: ResolveMode = "year_then_confidence", alpha: float = 0.7,
                 confidence_epsilon: float = 0.05, llm=None) -> None:
        self.mode = mode
        self.alpha = alpha
        self.confidence_epsilon = confidence_epsilon
        self.llm = llm

    def select(self, frame: QueryFrame, insights: list[dict], *,
               min_relevance: float = 0.5) -> list[dict]:
        scored = []
        for rec in insights:
            r = _relevance(rec, frame)
            if r >= min_relevance:
                scored.append({**rec, "relevance": r})
        scored.sort(key=lambda x: x["relevance"], reverse=True)
        return scored

    def resolve_conflicts(self, selected: list[dict]) -> list[dict]:
        """Group by Area; within a multi-insight Area rank a winner, mark the rest
        superseded. A conflict whose top two candidates are the SAME year and have
        near-equal confidence is flagged ``ambiguous`` — a deterministic tiebreak
        can't honestly pick, so it is left for adjudication (LLM) or kept unresolved.
        """
        if not selected:
            return []
        years = [r["year"] for r in selected if r.get("year") is not None]
        yr_range = (min(years), max(years)) if years else (0, 0)

        by_area: dict[str, list[dict]] = {}
        for rec in selected:
            by_area.setdefault(rec.get("area") or "(uncategorized)", []).append(rec)

        resolutions: list[dict] = []
        for area, recs in by_area.items():
            if len(recs) <= 1:
                continue
            ranked = sorted(
                recs, key=lambda r: _rank_value(r, self.mode, self.alpha, yr_range),
                reverse=True,
            )
            winner, *superseded = ranked
            ambiguous = self._is_ambiguous(winner, superseded[0]) if superseded else False
            resolutions.append({
                "area": area,
                "winner": winner,
                "superseded": superseded,
                "ambiguous": ambiguous,
                # default decision: pick (clear) / keep_both (ambiguous, no adjudication)
                "decision": "keep_both" if ambiguous else "pick",
                "rationale": self._rationale(winner, superseded, ambiguous),
            })
        return resolutions

    def _is_ambiguous(self, winner: dict, runner_up: dict) -> bool:
        same_year = winner.get("year") == runner_up.get("year")
        dconf = abs((winner.get("confidence") or 0) - (runner_up.get("confidence") or 0))
        return same_year and dconf <= self.confidence_epsilon

    def adjudicate(self, resolution: dict) -> dict:
        """Optional LLM adjudication of an ambiguous conflict. Returns the resolution
        with decision in {'pick','keep_both'} and winner updated. Deterministic
        fallback (no LLM / failure): keep_both (both retained, conflict unresolved)."""
        if not resolution.get("ambiguous") or self.llm is None:
            return resolution
        w, s = resolution["winner"], resolution["superseded"][0]
        data = self.llm.complete_json(_ADJ_SYS, _adj_user(w, s), _ADJ_SCHEMA)
        if not isinstance(data, dict):
            return resolution  # fallback: keep_both
        if data.get("keep_both") or not data.get("contradict", True):
            resolution["decision"] = "keep_both"
            resolution["rationale"] += " | LLM: retain both"
        elif data.get("winner_id") in {w["insight_id"], s["insight_id"]}:
            if data["winner_id"] == s["insight_id"]:
                resolution["winner"], resolution["superseded"][0] = s, w
            resolution["decision"] = "pick"
            resolution["rationale"] += f" | LLM picked {data['winner_id']}: {data.get('reason', '')}"
        return resolution

    def _rationale(self, winner: dict, superseded: list[dict], ambiguous: bool = False) -> str:
        if ambiguous:
            return (f"ambiguous: {winner.get('insight_id')} and "
                    f"{superseded[0].get('insight_id')} share FY{winner.get('year')} with "
                    f"near-equal confidence — kept both (unresolved) pending adjudication")
        if self.mode == "year_then_confidence":
            basis = f"FY{winner.get('year')} (newest), confidence {winner.get('confidence')}"
        else:
            basis = (f"blended score (α={self.alpha}): FY{winner.get('year')}, "
                     f"confidence {winner.get('confidence')}")
        sup = ", ".join(f"FY{s.get('year')}({s.get('confidence')})" for s in superseded)
        return f"selected {winner.get('insight_id')} by {basis}; superseded {sup}"

    def select_and_resolve(self, frame: QueryFrame, insights: list[dict], *,
                           min_relevance: float = 0.5
                           ) -> tuple[list[dict], list[dict]]:
        selected = self.select(frame, insights, min_relevance=min_relevance)
        resolutions = [self.adjudicate(r) for r in self.resolve_conflicts(selected)]
        # only 'pick' resolutions drop the superseded; 'keep_both' retains them
        superseded_ids = {
            s["insight_id"] for res in resolutions if res.get("decision") == "pick"
            for s in res["superseded"]
        }
        chosen = [r for r in selected if r["insight_id"] not in superseded_ids]
        return chosen, resolutions
