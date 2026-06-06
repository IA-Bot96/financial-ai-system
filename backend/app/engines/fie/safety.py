"""Numeric guard (L8a safety) — Phase 3.

Architecture §10.2 / §7.2: the LLM may make prose nicer, never introduce a number.
This module verifies that every numeric token in LLM-written prose is backed by an
in-scope value (a FactRef/CalcResult figure, a cited page/report year, an in-scope
fiscal year, or a small structural count). Any unbacked number → the prose is
rejected and the engine falls back to the deterministic renderer.
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import CalcResult, Citation, EvidenceItem, QueryFrame

_CITE_RE = re.compile(r"\[C\d+\]")  # citation handles contain digits — strip first
_UNIT_RE = re.compile(r"Rs\s*'?\s*000|Rupees in thousand|PKR", re.I)
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*[%x]?")

_REL_TOL = 0.01  # 1% — tolerant of rounding/formatting of the same figure


def _parse_token(tok: str) -> tuple[float, bool, bool]:
    """Return (magnitude, is_percent, is_integer_literal)."""
    t = tok.strip()
    is_pct = t.endswith("%")
    is_x = t.endswith("x")
    core = t.rstrip("%x").strip().replace(",", "")
    val = float(core)
    mag = val / 100.0 if is_pct else val
    is_int_literal = ("." not in core) and not is_pct and not is_x
    return mag, is_pct, is_int_literal


def extract_numbers(text: str) -> list[str]:
    cleaned = _CITE_RE.sub(" ", text)
    cleaned = _UNIT_RE.sub(" ", cleaned)
    out = []
    for m in _NUM_RE.finditer(cleaned):
        tok = m.group(0).strip()
        if re.search(r"\d", tok):
            out.append(tok)
    return out


def build_allowed(
    frame: QueryFrame,
    evidence: Iterable[EvidenceItem],
    calcs: Iterable[CalcResult],
    citations: Iterable[Citation],
) -> tuple[set[float], set[int]]:
    values: set[float] = set()
    ints: set[int] = set()

    def _add_value(v):
        if v is None:
            return
        values.add(round(float(v), 6))
        values.add(round(abs(float(v)), 6))

    evidence = list(evidence)
    calcs = list(calcs)
    citations = list(citations)

    for cr in calcs:
        _add_value(cr.value)
        for f in cr.inputs:
            _add_value(f.value)
    for e in evidence:
        _add_value(e.value)
        for f in e.fact_refs:
            _add_value(f.value)

    if frame.year:
        ints.add(int(frame.year))
    for e in evidence:
        for f in e.fact_refs:
            if f.year:
                ints.add(int(f.year))
    for c in citations:
        loc = c.locator or {}
        for k in ("page", "report_year", "year"):
            v = loc.get(k)
            if isinstance(v, (int, float)) and float(v).is_integer():
                ints.add(int(v))

    # structural counts the renderer may legitimately mention
    ints.update({0, 1, 2, len(evidence), len(citations)})
    return values, ints


def numbers_are_backed(text: str, allowed_values: set[float], allowed_ints: set[int],
                       rel_tol: float = _REL_TOL) -> bool:
    for tok in extract_numbers(text):
        try:
            mag, is_pct, is_int = _parse_token(tok)
        except ValueError:
            return False

        # in-scope fiscal year (always acceptable, not a financial claim)
        if is_int and 1900 <= mag <= 2100:
            continue
        if is_int and int(mag) in allowed_ints:
            continue
        if any(abs(mag - a) <= rel_tol * max(abs(a), 1.0) for a in allowed_values):
            continue
        return False  # an unbacked number appeared
    return True


def verify_prose(text: str, frame: QueryFrame, evidence, calcs, citations) -> bool:
    vals, ints = build_allowed(frame, evidence, calcs, citations)
    return numbers_are_backed(text, vals, ints)
