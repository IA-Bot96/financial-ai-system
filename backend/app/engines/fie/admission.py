"""Numeric admission role model (L6 governance).

Ported in spirit from the legacy FVE numeric-admission gate: every datum is assigned a
typed ROLE that governs *what it is allowed to do*, and the trust model is enforced
structurally — an external number can never be a baseline. The privileged role
(BASELINE) belongs solely to the uploaded workbook; external sources are admitted only
as supporting / event-fact / forecast-context, or excluded as non-authoritative.

Roles
  baseline           workbook financial fact (the source of truth) — may feed math
  event_fact         an authoritative event datum (e.g. a declared payout) — not a
                     statement-line baseline
  supporting         live market / overview / macro / regulatory context
  forecast_context   external datasets used for plausibility/peer comparison
                     (analysis reports, futures) — never a baseline
  non_authoritative  narrative / sentiment (news) — context only, creates no fact

The ``AdmissionDecision`` validator makes ``can_be_baseline=True`` unconstructable
unless ``role == baseline`` — so "external → baseline" cannot even be represented.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, model_validator


class NumericRole(str, Enum):
    BASELINE = "baseline"
    EVENT_FACT = "event_fact"
    SUPPORTING = "supporting"
    FORECAST_CONTEXT = "forecast_context"
    NON_AUTHORITATIVE = "non_authoritative"


# internal evidence kinds that ARE the workbook source of truth
_BASELINE_KINDS = {"statement", "detail", "calc"}

# external source-id token -> role (first substring match wins; case-insensitive)
_EXTERNAL_ROLE: tuple[tuple[str, NumericRole], ...] = (
    ("payout", NumericRole.EVENT_FACT),
    # analysis_reports = exchange-published *unaudited actuals* that overlap the
    # workbook's audited facts -> SUPPORTING (corroborates, never overrides). Not a
    # forecast: the datum is a historical actual, just from a lower-authority source.
    ("analysis_report", NumericRole.SUPPORTING),
    ("analysisreport", NumericRole.SUPPORTING),
    ("futures", NumericRole.FORECAST_CONTEXT),
    ("overview", NumericRole.SUPPORTING),
    ("quote", NumericRole.SUPPORTING),
    ("market_watch", NumericRole.SUPPORTING),
    ("marketwatch", NumericRole.SUPPORTING),
    ("screener", NumericRole.SUPPORTING),
    ("sector", NumericRole.SUPPORTING),
    ("summary", NumericRole.SUPPORTING),
    ("macro", NumericRole.SUPPORTING),
    ("secp", NumericRole.SUPPORTING),
    ("announcement", NumericRole.SUPPORTING),
)


def classify(source: Optional[str], kind: Optional[str], *, is_news: bool = False
             ) -> NumericRole:
    """Assign a role from the evidence kind + external source id."""
    if kind in _BASELINE_KINDS:
        return NumericRole.BASELINE
    if kind == "insight":
        return NumericRole.SUPPORTING            # internal qualitative, not a numeric baseline
    if is_news:
        return NumericRole.NON_AUTHORITATIVE
    s = (source or "").lower()
    for tok, role in _EXTERNAL_ROLE:
        if tok in s:
            return role
    return NumericRole.NON_AUTHORITATIVE          # unknown external -> conservative


def classify_evidence(ev) -> NumericRole:
    """Role for an EvidenceItem, reading its kind + citation locator (news is
    detected by the provider+link locator signature the news adapter writes)."""
    loc = ev.citations[0].locator if getattr(ev, "citations", None) else {}
    is_news = bool(loc.get("provider")) and bool(loc.get("link") or loc.get("url"))
    return classify(loc.get("source"), getattr(ev, "kind", None), is_news=is_news)


class AdmissionDecision(BaseModel):
    """The typed verdict for a datum. ``can_be_baseline`` is structurally constrained
    so an external number can never be admitted as a baseline."""

    source: Optional[str] = None
    role: NumericRole
    can_be_baseline: bool = False
    can_inform: bool = True          # may participate as context / supporting evidence
    reason: str = ""

    @model_validator(mode="after")
    def _invariant(self) -> "AdmissionDecision":
        if self.can_be_baseline and self.role != NumericRole.BASELINE:
            raise ValueError(
                "can_be_baseline=True requires role == baseline; external numbers "
                "can never be a baseline")
        return self


def admit(source: Optional[str], kind: Optional[str], *, is_news: bool = False
          ) -> AdmissionDecision:
    """Decide a datum's role and admissibility. Only BASELINE (the workbook) is
    baseline-eligible; NON_AUTHORITATIVE may still inform context but creates no fact."""
    role = classify(source, kind, is_news=is_news)
    return AdmissionDecision(
        source=source, role=role,
        can_be_baseline=(role is NumericRole.BASELINE),
        can_inform=True,
        reason=f"{kind or 'external'} -> {role.value}")


def is_baseline(ev) -> bool:
    """True only for workbook facts/derivations — the source of truth."""
    return classify_evidence(ev) is NumericRole.BASELINE


def audit(evidence) -> dict:
    """Role distribution over a list of EvidenceItems (for trace / coverage). Reads a
    stamped ``ev.role`` when present, else classifies on the fly."""
    out: dict[str, int] = {}
    for ev in evidence or []:
        stamped = getattr(ev, "role", None)
        r = stamped if isinstance(stamped, str) else classify_evidence(ev).value
        out[r] = out.get(r, 0) + 1
    return out
