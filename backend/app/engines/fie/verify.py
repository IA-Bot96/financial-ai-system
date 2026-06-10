"""Verification gate for composed answers — two independent checks.

1. numbers_ok — the existing numeric guard: every number in the prose must trace to a fetched
   value/calc. (Reused unchanged from safety.py.)

2. grounding_ok — a DETERMINISTIC scope check the numeric guard can't do: a number can be
   perfectly backed yet attached to the wrong subject. In the bake-off, "what is sector gross
   profit?" produced the COMPANY's gross-profit series relabeled "sector" — every figure was
   backed (they were the company's), so the numeric guard passed it. This check rejects an answer
   that asserts a SECTOR / PEER / INDUSTRY scope when the only evidence on hand is the subject
   company's own workbook (no external/peer-sourced evidence). It needs no LLM, so it's reliable
   on any model and can't be talked out of a rejection.
"""

from __future__ import annotations

import re

from . import safety

# scope words that imply data BEYOND the subject company's own workbook
_SCOPE_RE = re.compile(
    r"\b(sector|industry|peer|peers|competitor|competitors|rival|market[-\s]?wide|"
    r"across companies|other companies|benchmark|sector[-\s]average|industry[-\s]average)\b",
    re.I,
)

# qualitative-narrative claims that must come from report INSIGHTS/news, not be inferred from the
# numbers. The bake-off caught the model inventing "management is focused on growing revenue…" from the
# figures with zero citations — this gate refuses such a claim unless insight/external evidence
# actually backs it.
_QUALITATIVE_RE = re.compile(
    r"\bmanagement\b|\bchairman\b|\bstrateg|\bpriorit|\boutlook\b|\bguidance\b|\bcommentary\b|"
    r"\bmd&a\b|\bnarrative\b|\bqualitative\b|\bsentiment\b|\bdiscuss\w*\b|\bmentioned\b|"
    r"\bexplains?\b|\bcompetitive\b|\bmarket share\b",
    re.I,
)


def numbers_ok(text, frame, evidence, calcs, citations) -> bool:
    return safety.verify_prose(text, frame, evidence, calcs, citations)


def grounding_ok(text: str, evidence) -> tuple[bool, str]:
    """Return (ok, reason). Rejects two unbacked claim shapes the numeric guard can't catch:
    (1) scope — a sector/peer/industry claim with no external/peer evidence; (2) qualitative — a
    management/strategy/commentary claim with no insight or external evidence (i.e. narrative
    invented from the numbers)."""
    evs = evidence or []
    has_external = any(getattr(e, "kind", None) == "external" for e in evs)
    has_insight = any(getattr(e, "kind", None) == "insight" for e in evs)
    if _SCOPE_RE.search(text or "") and not has_external:
        return False, "scope"
    if _QUALITATIVE_RE.search(text or "") and not (has_insight or has_external):
        return False, "qualitative"
    return True, ""
