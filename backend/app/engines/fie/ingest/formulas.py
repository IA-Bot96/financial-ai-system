"""Lightweight intra-workbook formula evaluator.

The delivered workbooks were never recalculated in Excel, so statement cells like
``=F6+F7`` or ``=SUM('PL2 - Cost of Sales'!B5:B20)`` have no cached value under
data_only=True. This evaluator computes those values from the workbook itself —
the figures are correct, just uncached (architecture §0.3: trust the workbook).

Supports the only constructs the statements use: cell refs (local + sheet-
qualified), ranges, SUM(), unary minus, + - * /, and parentheses. Memoized with
cycle detection.
"""

from __future__ import annotations

import re
from typing import Optional

from openpyxl.utils import column_index_from_string, get_column_letter

# sheet-qualified cell/range:  'Sheet Name'!A1  or  Sheet!A1:B5
_SHEETCELL = re.compile(
    r"(?:'(?P<q>[^']+)'|(?P<u>[A-Za-z0-9_.&\- ]+))!(?P<ref>\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)"
)
# local cell or range: A1 or A1:B5
_LOCALREF = re.compile(r"\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_CELL = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)")


class WorkbookEvaluator:
    def __init__(self, wb_formulas, wb_data) -> None:
        self._wbf = wb_formulas
        self._wbd = wb_data
        self._cache: dict[tuple[str, str], Optional[float]] = {}
        self._visiting: set[tuple[str, str]] = set()

    # ---- public ----
    def value(self, sheet: str, coord: str) -> Optional[float]:
        key = (sheet, coord.replace("$", ""))
        if key in self._cache:
            return self._cache[key]
        if key in self._visiting:  # circular reference
            return None
        self._visiting.add(key)
        try:
            result = self._compute(sheet, coord)
        finally:
            self._visiting.discard(key)
        self._cache[key] = result
        return result

    # ---- internals ----
    def _compute(self, sheet: str, coord: str) -> Optional[float]:
        ws_d = self._wbd[sheet]
        cached = ws_d[coord].value
        if cached is not None and not (isinstance(cached, str) and cached.startswith("=")):
            return _num(cached)
        formula = self._wbf[sheet][coord].value
        if isinstance(formula, str) and formula.startswith("="):
            try:
                return self._eval(sheet, formula[1:])
            except Exception:
                return None
        return _num(formula)

    def _eval(self, sheet: str, expr: str) -> Optional[float]:
        py, ok = self._to_python(sheet, expr)
        if not ok:
            return None
        from ..calc.registry import FormulaError, safe_eval
        try:
            return float(safe_eval(py, {}))
        except (FormulaError, ZeroDivisionError, ValueError):
            return None

    def _to_python(self, sheet: str, expr: str) -> tuple[str, bool]:
        """Substitute SUM(), sheet refs, and local refs with numbers -> arithmetic."""
        # resolve SUM(...) innermost-first
        guard = 0
        while "SUM(" in expr.upper() and guard < 200:
            guard += 1
            m = re.search(r"SUM\(([^()]*)\)", expr, re.I)
            if not m:
                break
            total = 0.0
            for arg in m.group(1).split(","):
                total += self._resolve_ref_or_num(sheet, arg.strip())
            expr = expr[:m.start()] + repr(total) + expr[m.end():]

        # sheet-qualified refs
        def _sub_sheet(mm: re.Match) -> str:
            tgt = mm.group("q") or mm.group("u")
            return repr(self._resolve_ref_or_num(tgt.strip(), mm.group("ref")))
        expr = _SHEETCELL.sub(_sub_sheet, expr)

        # local refs
        def _sub_local(mm: re.Match) -> str:
            return repr(self._resolve_ref_or_num(sheet, mm.group(0)))
        expr = _LOCALREF.sub(_sub_local, expr)

        # only numbers, operators, parens, dots remain
        if re.fullmatch(r"[-+*/(). 0-9eE]*", expr or ""):
            return expr if expr.strip() else "0", True
        return "", False

    def _resolve_ref_or_num(self, sheet: str, ref: str) -> float:
        ref = ref.strip()
        if not ref:
            return 0.0
        if _NUMBER.fullmatch(ref):
            return float(ref)
        if ":" in ref:  # range -> sum of cells
            return self._sum_range(sheet, ref)
        v = self.value(sheet, ref.replace("$", ""))
        return float(v) if v is not None else 0.0

    def _sum_range(self, sheet: str, rng: str) -> float:
        a, b = rng.split(":")
        ca, ra = _CELL.match(a.replace("$", "")).groups()
        cb, rb = _CELL.match(b.replace("$", "")).groups()
        c0, c1 = sorted((column_index_from_string(ca), column_index_from_string(cb)))
        r0, r1 = sorted((int(ra), int(rb)))
        total = 0.0
        for ci in range(c0, c1 + 1):
            for ri in range(r0, r1 + 1):
                v = self.value(sheet, f"{get_column_letter(ci)}{ri}")
                if v is not None:
                    total += v
        return total


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None
