"""Claim-level citation enforcement (L8a).

Ported in spirit from the legacy query-engine ``v2_citation_enforcer``: every shipped
claim must cite at a precision its provenance actually supports, uncitable claims are
**dropped** (not weakened), and a findings-bearing answer that loses *all* its claims
degrades to a clean INSUFFICIENT_EVIDENCE response.

This complements ``safety.py`` — that guards NUMBERS in LLM prose; this guards CLAIMS
(the itemized key findings). Together they make "no citation, no claim" a per-claim
gate, not just a numeric one.

Precision is read from what the citation's locator can actually resolve:
  CELL  workbook sheet!cell        (financial)
  PAGE  report section + page      (insight)
  REF   a resolvable pointer       (workbook sheet-only, insight section/year,
                                     external source/link/date, derived/calc)
  NONE  empty/over-stated locator  -> not citable -> claim dropped
"""

from __future__ import annotations

import re

from .models import Citation

_HANDLE_RE = re.compile(r"\[(C\d+)\]")

# precision levels, high -> low
_RANK = {"CELL": 4, "PAGE": 3, "REF": 1, "NONE": 0}
_MIN_OK = _RANK["REF"]   # a claim ships only if its citation resolves at >= REF


def citation_precision(c: Citation) -> str:
    """The precision a citation's provenance actually supports."""
    loc = c.locator or {}
    kind = c.kind
    if kind == "financial":
        if loc.get("sheet") and loc.get("cell"):
            return "CELL"
        if any(loc.get(k) for k in ("sheet", "cell", "page", "table_id", "report_file")):
            return "REF"                      # partial workbook pointer — citable, weak
        return "NONE"
    if kind == "insight":
        if loc.get("page"):
            return "PAGE"
        if loc.get("source_section") or loc.get("year") or loc.get("insight_id"):
            return "REF"
        return "NONE"
    if kind == "external":
        if any(loc.get(k) for k in ("link", "url", "source", "date")):
            return "REF"                      # external claims cite a source/link, not a cell
        return "NONE"
    if kind in ("forecast", "calc"):
        return "REF"                          # derived; resolves through its inputs
    return "NONE"


def citation_ok(c: Citation) -> bool:
    """A citation is acceptable if its provenance resolves at >= REF precision."""
    return _RANK[citation_precision(c)] >= _MIN_OK


def valid_ref_ids(citations: list[Citation]) -> set[str]:
    """Inline handles (C1..Cn) whose citation has resolvable provenance."""
    return {c.ref_id for c in citations if citation_ok(c)}


def enforce_findings(findings: list[str], valid: set[str]) -> tuple[list[str], list[str]]:
    """Keep only findings whose ``[Cn]`` handle resolves to an acceptable citation.

    Returns ``(kept, dropped)``. A finding with no handle, a ``[—]`` placeholder, or a
    handle whose citation failed precision is dropped — never shipped uncited.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for f in findings:
        m = _HANDLE_RE.search(f)
        (kept if (m and m.group(1) in valid) else dropped).append(f)
    return kept, dropped
