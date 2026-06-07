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

import logging
from typing import Literal, Optional

from rapidfuzz import fuzz

from .models import Citation, EvidenceItem, QueryFrame

_log = logging.getLogger("app.engines.fie")

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


# Intent-specific signal vocabulary: an insight must contain at least one of these
# domain terms to be considered genuinely relevant. Without this guard, general
# query words like "historical", "revenue", "growth" partial-match almost every
# insight (ERP risk, AGM notices, board meetings, etc.) giving them a false 1.0 score.
_INTENT_SIGNALS: dict[str, list[str]] = {
    "forecast_validation": [
        "revenue", "sales", "growth", "margin", "profit", "forecast", "outlook",
        "demand", "volume", "inflation", "cost", "decline", "increase", "target",
        "market", "export", "liquidity", "earnings", "eps",
    ],
    "risk_assessment": [
        "risk", "margin", "pressure", "liquidity", "demand", "cost", "debt",
        "exposure", "default", "regulatory", "compliance", "operational",
    ],
    "trend_analysis": [
        "revenue", "sales", "growth", "margin", "profit", "trend", "decline",
        "increase", "volume", "earnings",
    ],
    "earnings_review": [
        "revenue", "sales", "profit", "margin", "earnings", "eps", "ebitda",
        "cost", "growth", "volume",
    ],
    "news_impact": [
        "market", "demand", "growth", "decline", "outlook", "price", "sales",
        "inflation", "policy", "announcement",
    ],
}


def _relevance(rec: dict, frame: QueryFrame) -> float:
    """0..1 relevance of an insight to the query.

    Uses exact substring containment (no fuzzy) for signal vocabulary to prevent
    false positives.  ``fuzz.partial_ratio`` was matching "cost" against "cont" in
    "continuity" with a 75% score because both words share the characters c, o, t at
    positions 0, 1, 3 — inflating ERP/AGM/governance noise well above the threshold.

    Scoring:
      signal_score — count of distinct signal terms that appear as **exact substrings**
        in the combined area+takeaway text, mapped to a 0..1 scale:
          0 matches → 0.00   (insight contains no domain vocabulary)
          1 match   → 0.55   (barely relevant; scores ~0.67 at threshold boundary)
          2 matches → 0.70   (moderately relevant; scores ~0.73)
          3+ matches → 0.70 + 0.05 × (n−3), capped at 1.0
      query_score — fuzz.token_set_ratio of raw query vs insight (secondary, weight=0.15)
      topical     = 0.85 × signal_score + 0.15 × query_score
      final       = 0.70 × topical      + 0.30 × temporal_affinity

    Consequence: 0 signal matches → final ≤ 0.35 (always filtered at min_relevance=0.65);
    1 match → final ≈ 0.67; 2+ matches → final ≥ 0.73.
    """
    text = " ".join(filter(None, [rec.get("area"), rec.get("takeaway")])).lower()
    if not text:
        return 0.0

    # Signal terms: intent vocabulary + explicit metric names from the frame
    signal_terms: list[str] = list(_INTENT_SIGNALS.get(frame.intent, []))
    signal_terms += [m.replace("_", " ") for m in (frame.metrics or [])]

    if signal_terms:
        # Exact substring containment — no fuzzy, prevents "cost" → "cont" false matches
        matched_terms = [t for t in signal_terms if t in text]
        n = len(matched_terms)
        if n == 0:
            signal_score = 0.0
        elif n == 1:
            signal_score = 0.55
        elif n == 2:
            signal_score = 0.70
        else:
            signal_score = min(0.70 + 0.05 * (n - 3), 1.0)
    else:
        # Unknown intent — fall back to raw query word containment
        words = [w for w in frame.raw_query.lower().split() if len(w) > 3]
        matched_terms = [w for w in words if w in text]
        signal_score = len(matched_terms) / max(len(words), 1) if words else 0.0

    # Full-query word-overlap score (minor secondary component, weight=0.15)
    query_score = fuzz.token_set_ratio(frame.raw_query.lower(), text) / 100.0

    # Signal vocabulary dominates: weight=0.85 ensures 0 matches → topical ≤ 0.06
    topical = 0.85 * signal_score + 0.15 * query_score

    # Temporal affinity: exact year match → neutral 1.0; mismatch → mild discount
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
               min_relevance: float = 0.65,
               top_k: int = 25) -> list[dict]:
        """Score and filter insights.

        ``min_relevance=0.65`` filters governance/ERP/AGM noise which scores ~0.34
        (0 matching signal terms) against financial queries.  Real financial insights
        typically match 2–6 domain terms and score 0.73–0.86.  ``top_k=25`` caps the
        output so the synthesizer stays focused — 89 premises for "write 1-3 sentences"
        is wasteful.
        """
        total = len(insights)
        all_scored: list[tuple[float, dict]] = []
        for rec in insights:
            r = _relevance(rec, frame)
            all_scored.append((r, rec))

        scored = [{**rec, "relevance": r} for r, rec in all_scored if r >= min_relevance]
        scored.sort(key=lambda x: x["relevance"], reverse=True)

        # Hard cap: keep only the most relevant top_k
        if top_k and len(scored) > top_k:
            dropped_by_cap = len(scored) - top_k
            scored = scored[:top_k]
        else:
            dropped_by_cap = 0

        filtered_out = total - len(scored) - dropped_by_cap
        signal_terms = list(_INTENT_SIGNALS.get(frame.intent, []))
        signal_terms += [m.replace("_", " ") for m in (frame.metrics or [])]

        _log.debug(
            "fie InsightSelector.select: query=%r intent=%s metrics=%s pool=%d "
            "min_relevance=%.2f passed=%d filtered_below_threshold=%d "
            "dropped_by_cap=%d(top_k=%d) signal_terms=%s",
            frame.raw_query[:80], frame.intent, frame.metrics,
            total, min_relevance, len(scored), filtered_out,
            dropped_by_cap, top_k, signal_terms[:8],
            extra={"component": "Insights"},
        )
        _sig = list(_INTENT_SIGNALS.get(frame.intent, []))
        _sig += [m.replace("_", " ") for m in (frame.metrics or [])]
        for i, r in enumerate(scored[:10]):
            _txt = " ".join(filter(None, [r.get("area"), r.get("takeaway")])).lower()
            _hits = [t for t in _sig if t in _txt]
            _log.debug(
                "  top[%d] %s area=%r yr=%s relevance=%.4f conf=%.2f hits=%s",
                i + 1, r.get("insight_id"), r.get("area"), r.get("year"),
                r["relevance"], r.get("confidence", 0), _hits[:8],
                extra={"component": "Insights"},
            )
        below = [(sc, rec) for sc, rec in all_scored if sc < min_relevance]
        if below:
            example = min(below, key=lambda t: t[0])
            _log.debug(
                "  %d below threshold (example: %s=%.4f area=%r)",
                len(below), example[1].get("insight_id"), example[0], example[1].get("area"),
                extra={"component": "Insights"},
            )
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
            decision = "keep_both" if ambiguous else "pick"
            resolutions.append({
                "area": area,
                "winner": winner,
                "superseded": superseded,
                "ambiguous": ambiguous,
                # default decision: pick (clear) / keep_both (ambiguous, no adjudication)
                "decision": decision,
                "rationale": self._rationale(winner, superseded, ambiguous),
            })
            _log.debug(
                "fie InsightSelector.resolve_conflicts: area=%r count=%d "
                "winner=%s(yr=%s conf=%.2f) superseded=[%s] ambiguous=%s decision=%s",
                area, len(recs),
                winner.get("insight_id"), winner.get("year"), winner.get("confidence", 0),
                ",".join(s.get("insight_id", "?") for s in superseded[:5]),
                ambiguous, decision,
                extra={"component": "Insights"},
            )
        _log.debug(
            "fie InsightSelector.resolve_conflicts: areas_with_conflict=%d total_resolutions=%d",
            len(resolutions), len(resolutions),
            extra={"component": "Insights"},
        )
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
                           min_relevance: float = 0.65,
                           top_k: int = 25,
                           ) -> tuple[list[dict], list[dict]]:
        selected = self.select(frame, insights, min_relevance=min_relevance, top_k=top_k)
        resolutions = [self.adjudicate(r) for r in self.resolve_conflicts(selected)]
        # only 'pick' resolutions drop the superseded; 'keep_both' retains them
        superseded_ids = {
            s["insight_id"] for res in resolutions if res.get("decision") == "pick"
            for s in res["superseded"]
        }
        chosen = [r for r in selected if r["insight_id"] not in superseded_ids]
        _log.debug(
            "fie InsightSelector.select_and_resolve: pool=%d selected=%d "
            "conflict_areas=%d superseded=%d -> final=%d",
            len(insights), len(selected), len(resolutions), len(superseded_ids), len(chosen),
            extra={"component": "Insights"},
        )
        return chosen, resolutions
