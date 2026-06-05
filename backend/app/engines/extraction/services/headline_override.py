"""Bucket B — headline face-truth override.

The output statements (P&L, Balance Sheet) are the deliverable; their breakdown
sheets are supporting notes. When a breakdown's leaves are incomplete or
mis-placed, the output cell that pulls/sums them won't tie out to the audited
TrustedFaceIndex (which we proved matches the PDF). For those HEADLINE cells we
substitute the audited face value directly — with a provenance comment — so the
delivered statements are correct even when the supporting detail is incomplete.

Scope guards keep this honest and narrow:
  - only the declared OUTPUT sheets (never breakdown notes),
  - only rows resolving to a KEY headline metric,
  - only (metric, year) pairs we actually have confident face truth for,
  - only cells that DON'T already tie out — a correct live formula is preserved.
Breakdown-sheet mismatches are intentionally left untouched and stay flagged in
the Validation Ledger as incomplete detail.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Override:
    sheet: str
    coordinate: str
    metric: str
    year: int
    was: object
    value: float
    source: str

    def __str__(self) -> str:
        return f"{self.sheet}!{self.coordinate} {self.metric}/{self.year}: {self.was!r} -> {self.value:,.0f}"


def override_headline_metrics(workbook_path, company, tieout, output_sheets,
                              annotate: bool = True) -> list[Override]:
    """Substitute audited face truth into failing headline cells on the output sheets.
    Returns the list of overrides (for logging / the manifest). Saves in place."""
    from openpyxl import load_workbook
    from openpyxl.cell.cell import MergedCell
    from openpyxl.comments import Comment

    from app.engines.extraction.pipeline.template_map import _KEY_METRICS, _row_metric
    from app.engines.extraction.services.face_truth import build_face_truth
    from app.engines.extraction.services.formula_eval import evaluate
    from app.engines.extraction.services.formula_repair import _year_columns
    from app.engines.extraction.services.validation import _src_str

    face = build_face_truth(company.tables)
    if not face or not output_sheets:
        return []
    wb = load_workbook(workbook_path, data_only=False)
    overrides: list[Override] = []
    for title in output_sheets:
        if title not in wb.sheetnames:
            continue
        ws = wb[title]
        header_row, band = _year_columns(ws)
        if not band:
            continue
        year_of = {c: int(ws.cell(header_row, c).value) for c in band}
        for r in range(header_row + 1, ws.max_row + 1):
            label = ws.cell(r, 1).value
            if not isinstance(label, str) or not label.strip():
                continue
            cm = _row_metric(label.strip())
            if cm not in _KEY_METRICS:
                continue
            for c in band:
                pair = face.get((cm, year_of[c]))
                if not pair:
                    continue
                truth, src = pair
                cell = ws.cell(r, c)
                if isinstance(cell, MergedCell):     # non-anchor of a merge — read-only
                    continue
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    computed = evaluate(wb, title, cell.coordinate)
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    computed = float(v)
                else:
                    computed = None
                # Output cells are signed; a cell already tying out keeps its formula.
                if computed is not None and tieout(computed, truth):
                    continue
                was = cell.value
                cell.value = float(truth)
                if annotate:
                    prior = f"{computed:,.0f}" if computed is not None else "blank / not evaluable"
                    cell.comment = Comment(
                        f"OVERRIDE: substituted audited {cm} = {truth:,.0f} (was {prior}). "
                        f"Source: {_src_str(src)}", "validation")
                overrides.append(Override(title, cell.coordinate, cm, year_of[c], was,
                                          float(truth), _src_str(src)))
    if overrides:
        wb.save(workbook_path)
    wb.close()
    return overrides
