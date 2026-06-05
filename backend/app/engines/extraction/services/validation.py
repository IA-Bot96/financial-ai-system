"""P4 — ValidatedOutputLedger.

Evaluates emitted key metrics (template plan OR no-template tables) against the
audited TrustedFaceIndex, produces an auditable ledger, and decides whether the
artifact is production-ready. A failing run is surfaced, not silently shipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.mapping import MappingPlan
from app.engines.extraction.services.face_truth import build_face_truth

_LEDGER_SHEET = "Validation Ledger"
_HEADERS = ["Status", "Sheet", "Cell/Label", "Metric", "Year", "Value", "Face truth", "Source", "Note"]


@dataclass
class LedgerRow:
    status: str          # ok | fallback | sign | WITHHELD | MISMATCH
    sheet: str
    where: str           # cell coordinate or row label
    metric: str
    year: Optional[int]
    value: Optional[float]
    face: Optional[float]
    source: str
    note: str

    def to_row(self) -> list:
        return [self.status, self.sheet, self.where, self.metric or "", self.year,
                self.value, self.face, self.source, self.note]


def _src_str(src) -> str:
    if not src:
        return ""
    rep = getattr(src, "report_file", "") or ""
    pages = getattr(src, "pages", None) or []
    tid = getattr(src, "table_id", None)
    return f"{rep} p{pages}" + (f" [{tid}]" if tid else "")


def template_ledger(plan: MappingPlan) -> list[LedgerRow]:
    """Ledger rows for a template run: every withheld value + a sample of writes."""
    rows: list[LedgerRow] = []
    for cw in plan.withheld:
        rows.append(LedgerRow("WITHHELD", cw.sheet, cw.coordinate, cw.matched_label,
                              cw.year, cw.value, None, "", cw.note or ""))
    for cw in plan.writes:
        note = cw.note or ""
        status = "fallback" if note.startswith("fallback") else ("sign" if note.startswith("sign") else "ok")
        if status != "ok":   # surface the noteworthy writes (fallbacks, sign-corrections)
            rows.append(LedgerRow(status, cw.sheet, cw.coordinate, cw.matched_label,
                                  cw.year, cw.value, None, "", note))
    return rows


def no_template_ledger(company: CompanyResult, tieout) -> tuple[list[LedgerRow], int]:
    """C3 — validate the no-template output's key metrics against face truth.

    Returns (rows, mismatch_count). Every line resolving to a headline metric is
    checked against the audited face value for that year."""
    from app.engines.extraction.pipeline.template_map import _KEY_METRICS
    face = build_face_truth(company.tables)
    rows: list[LedgerRow] = []
    mismatches = 0
    for t in company.tables:
        for li in t.line_items:
            cm = li.canonical_metric
            if cm not in _KEY_METRICS:
                continue
            for v in li.values:
                if v.year is None or v.value is None:
                    continue
                truth_pair = face.get((cm, v.year))
                if not truth_pair:
                    continue
                truth, src = truth_pair
                ok = tieout(v.value, truth)
                if not ok:
                    mismatches += 1
                rows.append(LedgerRow(
                    "ok" if ok else "MISMATCH", t.title or t.statement_type.value,
                    li.label, cm, v.year, v.value, truth, _src_str(v.source or src),
                    "" if ok else "does not tie out to audited face statement",
                ))
    return rows, mismatches


def computed_output_ledger(workbook_path, company: CompanyResult, tieout,
                           output_sheets: set | None = None,
                           annotate: bool = True) -> tuple[list[LedgerRow], int, int]:
    """#1 — evaluate the WRITTEN workbook's formula rows (output P&L / balance sheet
    and breakdown subtotals) and reconcile any that resolve to a headline metric to
    the audited face truth. Catches frozen-reference formulas and wrong computed
    totals that the leaf-level plan tie-out never sees.

    Returns (rows, fail_count, unevaluable_count). A key-metric formula the evaluator
    can't parse is counted as UNEVALUATED (surfaced, never silently treated as a
    pass). When `annotate`, MISMATCH/UNEVALUATED cells get an in-cell comment so the
    workbook flags untrusted values without destroying the template's formula.

    Sign convention is scoped explicitly: a formula is validated SIGNED when it sits
    on an OUTPUT statement (`output_sheets` = the template's computed/formula sheets)
    OR pulls cross-sheet (`'PL1'!B29`); otherwise it's a breakdown subtotal that
    keeps its own positive-magnitude convention and is compared by MAGNITUDE."""
    from openpyxl import load_workbook
    from openpyxl.comments import Comment

    from app.engines.extraction.pipeline.template_map import _KEY_METRICS, _row_metric
    from app.engines.extraction.services.face_truth import build_face_truth
    from app.engines.extraction.services.formula_eval import evaluate

    face = build_face_truth(company.tables)
    if not face:
        return [], 0, 0
    output_sheets = output_sheets or set()
    wb = load_workbook(workbook_path, data_only=False)
    rows: list[LedgerRow] = []
    fails = 0
    unevaluable = 0
    dirty = False
    for ws in wb.worksheets:
        # year-header columns (first row in the top 8 with >=2 year-like ints)
        best: dict[int, int] = {}
        hdr = 1
        for r in range(1, min(ws.max_row, 8) + 1):
            cols = {c: int(ws.cell(r, c).value) for c in range(1, ws.max_column + 1)
                    if isinstance(ws.cell(r, c).value, (int, float)) and 1990 <= int(ws.cell(r, c).value) <= 2100}
            if len(cols) > len(best):
                best, hdr = cols, r
        if len(best) < 2:
            continue
        for r in range(hdr + 1, ws.max_row + 1):
            label = ws.cell(r, 1).value
            if not isinstance(label, str) or not label.strip():
                continue
            cm = _row_metric(label.strip())
            if cm not in _KEY_METRICS:
                continue
            for c, year in best.items():
                cell = ws.cell(r, c)
                # only evaluate FORMULA cells (plain values were already plan-validated)
                if not (isinstance(cell.value, str) and cell.value.startswith("=")):
                    continue
                truth_pair = face.get((cm, year))
                if not truth_pair:
                    continue
                truth = truth_pair[0]
                computed = evaluate(wb, ws.title, cell.coordinate)
                if computed is None:
                    # Could not evaluate this formula -> surface it (NOT a silent pass).
                    unevaluable += 1
                    rows.append(LedgerRow("UNEVALUATED", ws.title, cell.coordinate, cm, year,
                                          None, truth, _src_str(truth_pair[1]),
                                          "formula not evaluable -> not validated"))
                    if annotate:
                        cell.comment = Comment(f"NOT VALIDATED: formula could not be evaluated "
                                               f"(audited {cm} = {truth:,.0f})", "validation")
                        dirty = True
                    continue
                # Signed on an output statement (whole-sheet) or any cross-sheet pull;
                # magnitude on intra-sheet breakdown subtotals (own positive convention).
                signed = ws.title in output_sheets or "!" in cell.value
                ok = tieout(computed, truth) if signed else tieout(abs(computed), abs(truth))
                if not ok:
                    fails += 1
                    if annotate:
                        cell.comment = Comment(f"VALIDATION FAILED: computed {computed:,.0f} does not "
                                               f"tie out to audited {cm} = {truth:,.0f}", "validation")
                        dirty = True
                rows.append(LedgerRow(
                    "ok" if ok else "MISMATCH", ws.title, cell.coordinate, cm, year,
                    round(computed, 2), truth, _src_str(truth_pair[1]),
                    "" if ok else "computed formula does not tie out to audited face statement",
                ))
    if dirty:
        wb.save(workbook_path)
    wb.close()
    return rows, fails, unevaluable


_SOURCE_HEADERS = ["Sheet", "Cell", "Template label", "Matched label", "Year", "Value",
                   "Report year", "Report file", "Page", "Table id", "Confidence", "Note"]


def write_source_ledger(workbook_path, plan: MappingPlan) -> None:
    """Traceability (#9): a 'Source Ledger' sheet mapping every written cell back to
    its report / page / table of origin."""
    from openpyxl import load_workbook

    from app.engines.extraction.services import styles as S
    wb = load_workbook(workbook_path)
    name = "Source Ledger"
    if name in wb.sheetnames:
        del wb[name]
    ws = wb.create_sheet(name)
    for c, h in enumerate(_SOURCE_HEADERS, start=1):
        cell = ws.cell(1, c, h)
        cell.font, cell.fill = S.HEADER_FONT, S.HEADER_FILL
    for r, w in enumerate(sorted(plan.writes, key=lambda x: (x.sheet, x.coordinate)), start=2):
        src = w.source
        pages = (src.pages if src else None) or []
        vals = [w.sheet, w.coordinate, w.template_label, w.matched_label, w.year, w.value,
                w.source_report_year, getattr(src, "report_file", None),
                (pages[0] if pages else None), getattr(src, "table_id", None),
                round(w.confidence, 3), w.note]
        for c, v in enumerate(vals, start=1):
            ws.cell(r, c, v)
    ws.freeze_panes = "A2"
    wb.save(workbook_path)


def append_ledger_sheet(workbook_path, rows: list[LedgerRow]) -> None:
    """Append a 'Validation Ledger' sheet to an existing workbook."""
    from openpyxl import load_workbook

    from app.engines.extraction.services import styles as S
    wb = load_workbook(workbook_path)
    if _LEDGER_SHEET in wb.sheetnames:
        del wb[_LEDGER_SHEET]
    ws = wb.create_sheet(_LEDGER_SHEET)
    for c, name in enumerate(_HEADERS, start=1):
        cell = ws.cell(1, c, name)
        cell.font, cell.fill = S.HEADER_FONT, S.HEADER_FILL
    for r, row in enumerate(sorted(rows, key=lambda x: (x.status == "ok", x.sheet)), start=2):
        for c, val in enumerate(row.to_row(), start=1):
            ws.cell(r, c, val)
    ws.freeze_panes = "A2"
    wb.save(workbook_path)
