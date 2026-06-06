"""Surface-don't-resolve divergence (L6).

The pattern three legacy engines (MSIL, FVE, QAE) independently converged on: when
sources disagree, **present both sides with an authority-weighted + chronology verdict**
rather than silently picking a winner. Truth is only *settled* when the trusted
workbook baseline is one of the sides (architecture: the workbook is authoritative by
assumption); every other disagreement is **surfaced** — the answer shows the
contradiction, who carries more authority (via the claim-type matrix) and which is
newer, but does not assert a winner.
"""

from __future__ import annotations

from typing import Optional

from . import authority
from .models import Conflict


def verdict(a, b, *, claim_type: Optional[authority.ClaimType] = None) -> dict:
    """Authority + chronology comparison of two evidence items about the same fact.
    Returns {authority_weighting, chronology, truth_resolution, surfaced}.

    truth_resolution is ``workbook_authoritative`` only when one side is the audited
    workbook baseline; otherwise ``not_determined`` (and ``surfaced=True``)."""
    ct = claim_type or authority.claim_type_for(a)
    ra = authority.effective_rank(ct, authority.authority_class_for(a))
    rb = authority.effective_rank(ct, authority.authority_class_for(b))
    ax = ra if ra is not None else 99
    bx = rb if rb is not None else 99
    if ax < bx:
        aw = "side_a_higher_authority"
    elif bx < ax:
        aw = "side_b_higher_authority"
    else:
        aw = "equal_authority"

    fa = getattr(a, "freshness", None) or getattr(a, "as_of", None) or ""
    fb = getattr(b, "freshness", None) or getattr(b, "as_of", None) or ""
    chronology = ("side_a_newer" if fa > fb else "side_b_newer" if fb > fa else "same_or_unknown")

    a_baseline = getattr(a, "role", None) == "baseline"
    b_baseline = getattr(b, "role", None) == "baseline"
    if a_baseline or b_baseline:
        truth = "workbook_authoritative"
        surfaced = False
    else:
        truth = "not_determined"
        surfaced = True
    return {"authority_weighting": aw, "chronology": chronology,
            "truth_resolution": truth, "surfaced": surfaced, "claim_type": ct.value}


def present(conflicts: list[Conflict]) -> str:
    """Human-facing summary of divergences: both sides + the verdict, distinguishing
    surfaced (no trusted baseline) from workbook-settled. Deterministic prose."""
    lines: list[str] = []
    for c in conflicts:
        vals = c.values or []
        if len(vals) < 2:
            continue
        sides = " vs ".join(
            f"{_fmt_side(v)}" for v in vals[:2])
        tag = "surfaced for review" if not c.resolved else "workbook authoritative"
        rationale = c.resolution or tag
        lines.append(f"Divergence on {c.topic}: {sides} — {rationale}.")
    return " ".join(lines)


def _fmt_side(v: dict) -> str:
    src = v.get("source") or "?"
    val = v.get("value")
    if isinstance(val, (int, float)):
        return f"{val:,.0f} ({src})"
    return f"{src}"
