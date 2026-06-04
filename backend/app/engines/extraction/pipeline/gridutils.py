"""Pure grid helpers for Layer 2: word clustering, orientation normalization,
year/currency detection, and multipage continuation checks.

No PDF/OCR dependencies here so the logic is unit-testable in isolation.
"""
from __future__ import annotations

import re

from app.engines.extraction.models.table import TableOrientation

# Match a 4-digit year not embedded in a longer digit run. Allows letter
# prefixes/suffixes like "FY2023" / "2024A" while rejecting "12025" or figures.
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_DIGIT_RE = re.compile(r"\d")
# A token that is a financial value: number (commas/decimals/parens/percent)
# or a dash placeholder meaning nil ('-', en/em dash, or the common mojibake '�').
_NUM_TOK_RE = re.compile(r"^[(\[]?[-+]?[\d,]+(?:\.\d+)?[)\]]?%?$")
_DASH_TOKENS = {"-", "–", "—", "�", "*"}

_CURRENCY_TOKENS = {
    "usd": "USD", "us$": "USD", "$": "USD",
    "eur": "EUR", "€": "EUR",
    "gbp": "GBP", "£": "GBP",
    "aed": "AED", "dhs": "AED",
    "pkr": "PKR", "rs.": "PKR", "rs": "PKR",
    "inr": "INR", "₹": "INR",
    "sar": "SAR", "jpy": "JPY", "¥": "JPY", "cny": "CNY",
}
_UNIT_TOKENS = [
    ("billion", "billions"), ("bn", "billions"),
    ("million", "millions"), ("mn", "millions"), ("'mn", "millions"),
    ("thousand", "thousands"), ("'000", "thousands"), ("000s", "thousands"),
]


# --- shape helpers ---

def rectangularize(rows: list[list[str]]) -> list[list[str]]:
    """Pad every row to the max width so the grid is rectangular."""
    if not rows:
        return rows
    width = max(len(r) for r in rows)
    return [list(r) + [""] * (width - len(r)) for r in rows]


def n_cols(rows: list[list[str]]) -> int:
    return max((len(r) for r in rows), default=0)


# --- word clustering (shared by native-words and OCR-words paths) ---

def _is_value_token(text: str) -> bool:
    t = text.strip()
    return t in _DASH_TOKENS or bool(_NUM_TOK_RE.match(t))


def _cluster_rows(words: list[dict], row_tol: float) -> list[list[dict]]:
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[list[dict]] = []
    current: list[dict] = [ordered[0]]
    ref_top = ordered[0]["top"]
    for w in ordered[1:]:
        if abs(w["top"] - ref_top) <= row_tol:
            current.append(w)
        else:
            rows.append(current)
            current = [w]
            ref_top = w["top"]
    rows.append(current)
    return rows


def cluster_words_to_grid(
    words: list[dict],
    row_tol: float = 8.0,
    col_tol: float = 25.0,
) -> list[list[str]]:
    """Reconstruct a table grid from positioned words (numeric-anchored).

    Financial statements have one wide left-hand *label* column followed by
    right-aligned *value* columns. Clustering every word by start-x fragments
    multi-word labels, so instead we:
      1. cluster rows by vertical proximity,
      2. derive value columns from the right edges of NUMERIC tokens only
         (numbers don't touch, so they cluster cleanly),
      3. merge everything left of the first value column into a single label
         cell, and assign each remaining token to its nearest value column.
    """
    if not words:
        return []

    rows = _cluster_rows(words, row_tol)

    nums = [w for w in words if _is_value_token(w["text"])]
    if not nums:
        # No numbers on the page -> each row is a single label cell.
        return [[" ".join(w["text"] for w in sorted(r, key=lambda w: w["x0"]))] for r in rows]

    # Cluster numeric tokens by right edge into candidate value columns.
    nums_sorted = sorted(nums, key=lambda w: w["x1"])
    clusters: list[list[dict]] = [[nums_sorted[0]]]
    for w in nums_sorted[1:]:
        if w["x1"] - clusters[-1][-1]["x1"] > col_tol:
            clusters.append([w])
        else:
            clusters[-1].append(w)

    # Keep only columns that recur across rows; this drops stray numbers in
    # titles/dates (e.g. "June 30, 2025") that would otherwise collapse the
    # label region. Fall back to all clusters if filtering removes everything.
    min_members = max(2, int(0.1 * len(rows)))
    kept = [c for c in clusters if len(c) >= min_members] or clusters
    kept.sort(key=lambda c: sum(t["x1"] for t in c) / len(c))

    col_right = [sum(t["x1"] for t in c) / len(c) for c in kept]
    label_boundary = min(t["x0"] for t in kept[0]) - 2.0

    def nearest_col(x1: float) -> int:
        return min(range(len(col_right)), key=lambda i: abs(col_right[i] - x1))

    grid: list[list[str]] = []
    for row in rows:
        cells = [""] * (len(col_right) + 1)  # index 0 = label
        labels: list[str] = []
        for w in sorted(row, key=lambda w: w["x0"]):
            if w["x1"] <= label_boundary:
                labels.append(w["text"])
            else:
                ci = nearest_col(w["x1"]) + 1
                cells[ci] = (cells[ci] + " " + w["text"]).strip()
        cells[0] = " ".join(labels)
        grid.append(cells)
    return _drop_empty_columns(grid)


def _drop_empty_columns(grid: list[list[str]]) -> list[list[str]]:
    """Remove columns that are empty in every row (e.g. left-margin artifacts)."""
    if not grid:
        return grid
    width = max(len(r) for r in grid)
    padded = [r + [""] * (width - len(r)) for r in grid]
    keep = [i for i in range(width) if any(r[i].strip() for r in padded)]
    return [[r[i] for i in keep] for r in padded]


def has_labels(grid: list[list[str]]) -> bool:
    """True if the first column carries text labels (not just numbers/blanks).

    Used to reject ruled-table extractions that captured only the numeric
    columns and dropped the row labels.
    """
    nonempty = [r for r in grid if any(c.strip() for c in r)]
    if not nonempty:
        return False
    labelled = sum(1 for r in nonempty if r and r[0].strip() and not _is_value_token(r[0]))
    return labelled >= max(2, len(nonempty) // 2)


def looks_tabular(rows: list[list[str]], min_numeric_ratio: float = 0.15) -> bool:
    """Heuristic guard so prose isn't misread as a table."""
    if len(rows) < 2 or n_cols(rows) < 2:
        return False
    cells = [c for r in rows for c in r if c.strip()]
    if not cells:
        return False
    numeric = sum(1 for c in cells if _DIGIT_RE.search(c))
    return (numeric / len(cells)) >= min_numeric_ratio


# --- orientation ---

def _count_year_cells(cells: list[str]) -> int:
    return sum(1 for c in cells if c and _YEAR_RE.search(c))


def detect_and_normalize(rows: list[list[str]]) -> tuple[list[list[str]], TableOrientation]:
    """Return (canonical_rows, original_orientation).

    Canonical = line-item labels in column 0, periods/years across the header.
    If years run *down* the first column instead, the table is transposed.
    """
    rows = rectangularize(rows)
    if len(rows) < 2:
        return rows, TableOrientation.unknown

    header = rows[0]
    first_col = [r[0] for r in rows[1:]]
    years_across = _count_year_cells(header)
    years_down = _count_year_cells(first_col)

    if years_across >= 2 and years_across >= years_down:
        return rows, TableOrientation.vertical
    if years_down >= 2 and years_down > years_across:
        transposed = [list(col) for col in zip(*rows)]
        return transposed, TableOrientation.horizontal
    return rows, TableOrientation.vertical


# --- year / currency / unit extraction ---

def extract_years(header: list[str]) -> list[int]:
    years: set[int] = set()
    for cell in header:
        for m in _YEAR_RE.finditer(cell or ""):
            years.add(int(m.group()))
    return sorted(years)


def detect_currency_unit(*texts: str) -> tuple[str | None, str | None]:
    blob = " ".join(t for t in texts if t).lower()
    currency = None
    for token, code in _CURRENCY_TOKENS.items():
        if token in blob:
            currency = code
            break
    unit = None
    for token, scale in _UNIT_TOKENS:
        if token in blob:
            unit = scale
            break
    return currency, unit


# --- multipage continuation ---

def has_year_header(rows: list[list[str]]) -> bool:
    return bool(rows) and _count_year_cells(rows[0]) >= 1


def is_continuation(
    prev_rows: list[list[str]],
    prev_pages: list[int],
    cur_rows: list[list[str]],
    cur_pages: list[int],
    same_section: bool,
) -> bool:
    """Decide whether `cur` is a continuation of `prev` onto the next page(s)."""
    if not prev_pages or not cur_pages:
        return False
    if min(cur_pages) - max(prev_pages) != 1:  # must be the immediately next page
        return False
    if n_cols(prev_rows) != n_cols(cur_rows):
        return False
    if not same_section:
        return False
    # A continuation typically resumes with data, not a fresh year header.
    if has_year_header(cur_rows):
        return False
    return True
