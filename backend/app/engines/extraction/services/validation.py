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
