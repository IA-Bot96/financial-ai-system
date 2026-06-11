"""Parse Source Ledger and Validation Ledger sheets into DataFrames.

The Source Ledger is the provenance backbone (detail sheets only). The
Validation Ledger is loaded for the dev-time validator and is not used on the
runtime answer path (architecture §0.3).

See docs/fie_phase0_foundation.md §2.4, §2.5.
"""

from __future__ import annotations

import pandas as pd

from ..ontology import normalize_label


def _rows_to_df(ws) -> pd.DataFrame:
    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    headers = [str(h).strip() if h is not None else f"col{c}" for c, h in enumerate(headers)]
    data = []
    for r in range(2, ws.max_row + 1):
        row = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
        if all(v is None for v in row):
            continue
        data.append(row)
    return pd.DataFrame(data, columns=headers)


def parse_source_ledger(ws) -> pd.DataFrame:
    """Columns: Sheet, Cell, Template label, Matched label, Year, Value,
    Report year, Report file, Page, Table id, Confidence, Note.

    Adds ``matched_label_norm`` for joining and a composite ``source_ref``.
    """
    df = _rows_to_df(ws)
    if df.empty:
        return df
    # normalized matched label for metric joins
    label_col = "Matched label" if "Matched label" in df.columns else None
    if label_col:
        df["matched_label_norm"] = df[label_col].map(normalize_label)

    def _ref(row) -> str | None:
        rf, pg, tid = row.get("Report file"), row.get("Page"), row.get("Table id")
        if rf is None:
            return None
        if tid:
            return str(tid)
        return f"{rf}:p{pg}" if pg is not None else str(rf)

    df["source_ref"] = df.apply(_ref, axis=1)
    return df


def parse_validation_ledger(ws) -> pd.DataFrame:
    """Columns: Status, Sheet, Cell/Label, Metric, Year, Value, Face truth,
    Source, Note. Dev-only (architecture §0.3)."""
    return _rows_to_df(ws)
