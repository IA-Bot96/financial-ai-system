"""Sheet classification for FIE ingestion — general across workbook structures.

Handles both the templated workbook (P&L, Balance Sheet, PL1-7, BS1-5, ledgers)
and arbitrary OCR "no-template" workbooks where each statement/note is its own
title-named sheet. Anything with a year-header data grid is parseable; the
statement *family* is inferred from the title.

See docs/fie_phase0_foundation.md §2.1.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

SheetClass = Literal[
    "separator", "statement", "detail", "insights", "insights_review",
    "source_ledger", "validation_ledger", "freetext", "unknown",
]
Family = Optional[Literal["pl", "bs", "cf", "equity"]]

_SEPARATOR_RE = re.compile(r"[>]{2,}|-{3,}")
_DETAIL_RE = re.compile(r"^(PL|BS)\d", re.IGNORECASE)

# title keyword -> statement family (checked in order; cash before the rest)
_FAMILY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"cash\s*flow|statement of cas|cashflow", re.I), "cf"),
    (re.compile(r"profit or loss|profit and loss|income statement|statement of pro|\bp&l\b|profit/?\(loss\)", re.I), "pl"),
    (re.compile(r"financial position|balance sheet|statement of fin", re.I), "bs"),
    (re.compile(r"changes in equity|statement of cha", re.I), "equity"),
]
# a sheet is a *headline statement* (not a note) if its title looks like a primary statement
_MAIN_STATEMENT_RE = re.compile(
    r"statement of|balance sheet|income statement|\bp&l\b|six years|financial highlights", re.I)


def statement_family(title: str) -> Family:
    t = (title or "").strip()
    if _DETAIL_RE.match(t):
        return "bs" if t.upper().startswith("BS") else "pl"
    for pat, fam in _FAMILY_PATTERNS:
        if pat.search(t):
            return fam
    return None


def classify_sheet(title: str) -> SheetClass:
    t = (title or "").strip()
    if _SEPARATOR_RE.search(t):
        return "separator"
    if t == "Source Ledger":
        return "source_ledger"
    if t == "Validation Ledger":
        return "validation_ledger"
    if t == "Insights":
        return "insights"
    if t == "Insights Review":
        return "insights_review"
    if t in {"Mgmt info.", "Qualtitative Data", "Qualitative Data"}:
        return "freetext"
    # templated main statements
    if t in {"P&L", "Balance Sheet"}:
        return "statement"
    # templated detail
    if _DETAIL_RE.match(t):
        return "detail"
    # general: a primary statement title -> headline statement; else a generic table
    if statement_family(t) and _MAIN_STATEMENT_RE.search(t):
        return "statement"
    return "detail"  # default: attempt to parse as a generic table (skipped if no grid)


def statement_of(title: str) -> Literal["pl", "bs", "cf", "equity", "other"]:
    """Statement family for a statement/detail sheet (defaults to 'other')."""
    fam = statement_family(title)
    return fam or "other"
