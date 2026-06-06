"""Confidence scoring (L7).

Min-weakest-link composition (ported in spirit from FVE's ConfidenceComposer / QAE's
mapping-confidence): the final score is ``min`` over a set of named components — an
evidence-quality base plus a series of ceilings — and the **binding (lowest)** component
is reported as ``limited_by`` so the band is explainable. An answer can never be more
confident than its weakest link.

Runtime confidence concerns evidence strength, corroboration, and coverage — NOT the
correctness of the financial core (trusted by assumption). There is no financial-mismatch
or reconciliation cap at runtime (architecture §0.3 / §9).
"""

from __future__ import annotations

from .models import CalcResult, Conflict, ConfidenceReport, EvidenceItem

_BAND_SCORE = {"High": 0.9, "Medium": 0.6, "Low": 0.3}
_SCORE_BAND = [(0.8, "High"), (0.5, "Medium"), (0.0, "Low")]
_LOW_INSIGHT_CONF = 0.6


def _band_from_score(score: float) -> str:
    for threshold, band in _SCORE_BAND:
        if score >= threshold:
            return band
    return "Low"


class ConfidenceScorer:
    def score(self, *, evidence: list[EvidenceItem], calcs: list[CalcResult],
              conflicts: list[Conflict], selected_insights: list[dict],
              degraded: bool = False, partial_coverage: bool = False
              ) -> ConfidenceReport:
        reasons: list[str] = []
        caps: list[str] = []
        components: list[dict] = []
        MED = _BAND_SCORE["Medium"]

        def _cap(value: float, rationale: str, name: str) -> None:
            """Record a ceiling component (and keep the legacy caps_applied string)."""
            components.append({"name": name, "value": value, "rationale": rationale})
            caps.append(rationale)

        # base component: evidence-quality floor from calcs / financial evidence
        if calcs:
            base = min(_BAND_SCORE[c.confidence] for c in calcs)
            base_reason = "financial inputs sourced from the workbook (authoritative)"
            if any(c.value is not None for c in calcs):
                reasons.append(base_reason)
        elif evidence:
            cited = [e for e in evidence if e.citations]
            base = 0.9 if cited else 0.6
            base_reason = "evidence cited to the workbook" if cited else "uncited evidence"
            if cited:
                reasons.append(base_reason)
        elif selected_insights:
            base, base_reason = 0.6, "insight-only evidence"
        else:
            reason = ("required external source unavailable; no internal fallback"
                      if degraded else "no supporting evidence found")
            return ConfidenceReport(band="Low", score=0.3, reasons=[reason],
                                    limited_by=reason,
                                    components=[{"name": "evidence_quality",
                                                 "value": 0.3, "rationale": reason}])
        components.append({"name": "evidence_quality", "value": base, "rationale": base_reason})

        # --- ceilings (no financial-mismatch cap; §0.3) ---
        if degraded:
            _cap(MED, "required external source unavailable (degraded)", "degraded")
        if partial_coverage:
            _cap(MED, "partial coverage", "partial_coverage")

        if selected_insights:
            top = selected_insights[0]
            if (top.get("confidence") or 0.0) < _LOW_INSIGHT_CONF:
                _cap(MED, f"top insight confidence {top.get('confidence')} < {_LOW_INSIGHT_CONF}",
                     "low_top_insight")

        unresolved = [c for c in conflicts if not c.resolved]
        if unresolved:
            _cap(MED, f"{len(unresolved)} unresolved conflict(s)", "unresolved_conflicts")
        elif conflicts:
            reasons.append(f"{len(conflicts)} conflict(s) detected and resolved")

        # single uncorroborated source -> at most Medium
        sources = {(c.locator.get("report_file"), c.locator.get("page"))
                   for e in evidence for c in e.citations}
        if calcs:
            sources |= {(c.locator.get("report_file"), c.locator.get("page"))
                        for cr in calcs for c in cr.citations}
        if len(sources) <= 1 and not selected_insights:
            _cap(MED, "single uncorroborated source", "single_source")

        # admission: an answer resting ONLY on non-authoritative evidence (news /
        # sentiment) is context, not fact — cap at Medium however well cited it is.
        if evidence and not calcs and all(
                getattr(e, "role", None) == "non_authoritative" for e in evidence):
            _cap(MED, "non-authoritative sources only", "non_authoritative")

        # min-weakest-link: the lowest component binds the score
        binding = min(components, key=lambda c: c["value"])
        score = round(binding["value"], 3)
        return ConfidenceReport(band=_band_from_score(score), score=score,
                                reasons=reasons, caps_applied=caps,
                                limited_by=binding["rationale"], components=components)
