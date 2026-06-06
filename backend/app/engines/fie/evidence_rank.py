"""Deterministic evidence ranker (L6).

Ported in spirit from the legacy query-engine ``v2_evidence_ranker``: a transparent,
multi-signal score that orders evidence for presentation/selection, with
provenance-completeness dominant, then authority (the admission role), recency, and the
source's reliability. Pure and deterministic — no LLM, stable ordering.

    score = 100·provenance + 20·authority + 10·recency + 5·reliability

Used to order the evidence that feeds list-style findings (risk / news / earnings) so
the most authoritative, best-sourced, freshest items surface first.
"""

from __future__ import annotations

from typing import Optional

from . import admission, citation_enforce
from .models import EvidenceItem

# authority by admission role (workbook baseline highest → news lowest)
_ROLE_AUTHORITY = {
    "baseline": 1.0,
    "event_fact": 0.85,
    "supporting": 0.70,
    "forecast_context": 0.60,
    "non_authoritative": 0.40,
}
# provenance completeness by citation precision (see citation_enforce)
_PRECISION_W = {"CELL": 1.0, "PAGE": 0.8, "REF": 0.5, "NONE": 0.0}


def _recency(ev: EvidenceItem) -> float:
    """0..1 from the item's freshness/as_of year; neutral 0.5 when unknown. Monotonic
    (newer → higher); absolute scale is deliberately coarse since provenance/authority
    dominate the score."""
    raw = ev.freshness or ev.as_of
    if not raw:
        return 0.5
    s = str(raw)
    year = None
    for i in range(len(s) - 3):
        chunk = s[i:i + 4]
        if chunk.isdigit():
            y = int(chunk)
            if 1990 <= y <= 2100:
                year = y
                break
    if year is None:
        return 0.5
    return max(0.0, min(1.0, (year - 2015) / 15.0))


def _authority(ev: EvidenceItem) -> float:
    role = ev.role or admission.classify_evidence(ev).value
    return _ROLE_AUTHORITY.get(role, 0.4)


def _provenance(ev: EvidenceItem) -> float:
    if not ev.citations:
        return 0.0
    return _PRECISION_W[citation_enforce.citation_precision(ev.citations[0])]


def score(ev: EvidenceItem) -> float:
    """Deterministic multi-signal score (higher = surfaces first)."""
    rel = ev.reliability if ev.reliability is not None else 1.0
    return round(100 * _provenance(ev) + 20 * _authority(ev)
                 + 10 * _recency(ev) + 5 * rel, 4)


def rank(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    """Stable sort by descending score (ties keep input order)."""
    return sorted(evidence, key=score, reverse=True)


def top(evidence: list[EvidenceItem], k: Optional[int] = None) -> list[EvidenceItem]:
    """Top-k ranked evidence (all of it if k is None)."""
    ranked = rank(evidence)
    return ranked if k is None else ranked[:k]
