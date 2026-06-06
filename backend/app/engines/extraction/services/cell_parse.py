"""Deterministic accounting-number parsing — a COMPLEMENT to GPT/rule extraction.

GPT reconstructs the table (which cell -> which metric/year, role, components) and
returns both a parsed `value` and the printed `raw` token. This module re-parses that
raw token deterministically so we can HARDEN the number (sign + format) where the LLM
slipped — it never re-extracts or re-maps anything. Ported/adapted from the legacy
engine's `parse_number` (parentheses-negative, unicode-minus, currency/scale stripping,
nil/dash detection).

Design rules (kept conservative on purpose):
  * Magnitude is NEVER changed here (no scale multiplication) — that avoids the classic
    1000x error and keeps us in the report's printed unit, exactly like GPT.
  * Sign is only *corrected*, and only in the unambiguous direction (raw clearly shows a
    negative marker but the parsed value came back positive).
"""
from __future__ import annotations

import re

# Standalone "empty / nil" cell markers (any dash variant, NA, nil, ...). A dash that
# sits on its own means no value; a dash/minus glued to digits is a negative sign.
_EMPTY_TOKENS = {
    "", "-", "‐", "‑", "‒", "–", "—", "―", "−",
    ".", "..", "...", "n/a", "na", "nil", "none", "—", "–",
}
# Currency words/symbols stripped before number extraction.
_CURRENCY_RE = re.compile(r"(?i)\b(?:rs|rupees|pkr|usd|inr|gbp|eur|aed|us\$)\b|[₨$£€¥]")
# A negative is indicated by accounting parentheses around digits, or a minus/unicode-
# minus immediately preceding digits. (En/em dashes are treated as empty, not minus.)
_PAREN_NEG_RE = re.compile(r"\(\s*[\d,]")
_MINUS_NEG_RE = re.compile(r"[-−]\s*\d")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_money(token) -> tuple[float | None, bool]:
    """Parse an accounting cell into (value, raw_is_negative).

    Returns (None, False) for empty/nil/dash cells or anything with no digits.
    `raw_is_negative` reports what the printed token indicates, independent of the
    returned value's sign — callers use it to reconcile a dropped sign."""
    if token is None:
        return None, False
    s = str(token).strip()
    if s.lower() in _EMPTY_TOKENS:
        return None, False

    negative = bool(_PAREN_NEG_RE.search(s) or _MINUS_NEG_RE.search(s))
    cleaned = _CURRENCY_RE.sub("", s).replace(",", "").replace("%", "")
    cleaned = cleaned.replace("−", "-")
    m = _NUMBER_RE.search(cleaned)
    if not m:
        return None, False
    try:
        magnitude = float(m.group(0))
    except ValueError:
        return None, False
    return (-magnitude if negative else magnitude), negative


def _note_int(note_ref) -> int | None:
    """The integer a note reference denotes, e.g. '12' / 'Note 12' -> 12; else None."""
    if note_ref is None:
        return None
    m = re.search(r"\d+", str(note_ref))
    return int(m.group(0)) if m else None


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.5, 0.001 * abs(b))


def normalize_table_values(table) -> dict:
    """Harden the values of one FinancialTable in place, using the printed `raw` token.
    Complements (never replaces) extraction. Returns a small counts dict for logging.

    Two corrections, both conservative:
      * SIGN: if `raw` unambiguously shows a negative (parentheses / minus) but the
        parsed value came back POSITIVE at the same magnitude, flip it. (The reverse —
        forcing a negative to positive — is NOT done, to respect `is_contra` costs that
        are intentionally printed positive.)
      * NOTE-REF: drop a value that exactly equals the line's note reference, is a small
        integer, and is a >10x outlier vs the line's other-year values — i.e. a note
        number that leaked into a value column. Heavily guarded to avoid dropping a real
        small figure.
    Magnitude is never otherwise changed (no scale multiplication)."""
    counts = {"sign_fixed": 0, "note_ref_dropped": 0}
    for li in table.line_items:
        note_int = _note_int(getattr(li, "note_ref", None))
        siblings = [abs(v.value) for v in li.values if v.value is not None]
        for v in li.values:
            if v.value is None:
                continue
            # --- sign reconciliation from the printed token ---
            if v.raw:
                parsed, raw_neg = parse_money(v.raw)
                if parsed is not None and _close(abs(parsed), abs(v.value)):
                    if raw_neg and v.value > 0:
                        v.value = -abs(v.value)
                        counts["sign_fixed"] += 1
            # --- note-reference leak drop (guarded) ---
            if (note_int is not None and float(v.value).is_integer()
                    and abs(v.value) == note_int and abs(v.value) < 100):
                others = [s for s in siblings if s != abs(v.value)]
                if others and abs(v.value) < 0.1 * min(others):
                    v.value = None
                    v.raw = None
                    counts["note_ref_dropped"] += 1
    return counts
