"""Confidence scoring (L7) — Phase 2.

Bottom-up, transparent weighted band with hard caps. Runtime confidence concerns
insight strength, corroboration, and coverage — NOT the correctness of the
financial core (trusted by assumption). There is no financial-mismatch or
reconciliation cap at runtime (architecture §0.3 / §9).
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

        # base: bottom-up from calc results / financial evidence
        if calcs:
            base = min(_BAND_SCORE[c.confidence] for c in calcs)
            if any(c.value is not None for c in calcs):
                reasons.append("financial inputs sourced from the workbook (authoritative)")
        elif evidence:
            cited = [e for e in evidence if e.citations]
            base = 0.9 if cited else 0.6
            if cited:
                reasons.append("evidence cited to the workbook")
        elif selected_insights:
            base = 0.6
        else:
            reason = ("required external source unavailable; no internal fallback"
                      if degraded else "no supporting evidence found")
            return ConfidenceReport(band="Low", score=0.3, reasons=[reason])

        score = base
        cap_ceiling = 1.0

        # --- caps (no financial-mismatch cap; §0.3) ---
        if degraded:
            cap_ceiling = min(cap_ceiling, _BAND_SCORE["Medium"])
            caps.append("required external source unavailable (degraded)")
        if partial_coverage:
            cap_ceiling = min(cap_ceiling, _BAND_SCORE["Medium"])
            caps.append("partial coverage")

        if selected_insights:
            top = selected_insights[0]
            if (top.get("confidence") or 0.0) < _LOW_INSIGHT_CONF:
                cap_ceiling = min(cap_ceiling, _BAND_SCORE["Medium"])
                caps.append(f"top insight confidence {top.get('confidence')} < {_LOW_INSIGHT_CONF}")

        unresolved = [c for c in conflicts if not c.resolved]
        if unresolved:
            cap_ceiling = min(cap_ceiling, _BAND_SCORE["Medium"])
            caps.append(f"{len(unresolved)} unresolved conflict(s)")
        elif conflicts:
            reasons.append(f"{len(conflicts)} conflict(s) detected and resolved")

        # single uncorroborated source -> at most Medium
        sources = {
            (c.locator.get("report_file"), c.locator.get("page"))
            for e in evidence for c in e.citations
        }
        if calcs:
            sources |= {
                (c.locator.get("report_file"), c.locator.get("page"))
                for cr in calcs for c in cr.citations
            }
        if len(sources) <= 1 and not selected_insights:
            cap_ceiling = min(cap_ceiling, _BAND_SCORE["Medium"])
            caps.append("single uncorroborated source")

        # admission: an answer resting ONLY on non-authoritative evidence (news /
        # sentiment) is context, not fact — cap at Medium however well cited it is.
        if evidence and not calcs and all(
                getattr(e, "role", None) == "non_authoritative" for e in evidence):
            cap_ceiling = min(cap_ceiling, _BAND_SCORE["Medium"])
            caps.append("non-authoritative sources only")

        score = min(score, cap_ceiling)
        band = _band_from_score(score)
        return ConfidenceReport(band=band, score=round(score, 3),
                                reasons=reasons, caps_applied=caps)
