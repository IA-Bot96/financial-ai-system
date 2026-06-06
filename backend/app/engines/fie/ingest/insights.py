"""Parse Insights / Insights Review sheets into JSON records.

Text-heavy and consumed whole / handed to the LLM, so represented as a list of
dicts rather than a DataFrame (docs/fie_phase0_foundation.md §2.6, §3).
"""

from __future__ import annotations

from typing import Optional

_FIELD_MAP = {
    "year": "year",
    "source report year": "source_report_year",
    "area": "area",
    "takeaway": "takeaway",
    "source section": "source_section",
    "page": "page",
    "confidence": "confidence",
}


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_insights(ws, *, id_prefix: str = "INS") -> list[dict]:
    """Return one record per data row with a stable ``insight_id``."""
    # map columns by normalized header
    col_field: dict[int, str] = {}
    for c in range(1, ws.max_column + 1):
        h = ws.cell(1, c).value
        key = str(h).strip().lower() if h is not None else ""
        if key in _FIELD_MAP:
            col_field[c] = _FIELD_MAP[key]

    records: list[dict] = []
    for r in range(2, ws.max_row + 1):
        row = {field: ws.cell(r, c).value for c, field in col_field.items()}
        if not any(v is not None for v in row.values()):
            continue
        rec = {
            "insight_id": f"{id_prefix}-{r}",
            "year": _to_int(row.get("year")),
            "source_report_year": _to_int(row.get("source_report_year")),
            "area": (str(row["area"]).strip() if row.get("area") else None),
            "takeaway": (str(row["takeaway"]).strip() if row.get("takeaway") else None),
            "source_section": (str(row["source_section"]).strip() if row.get("source_section") else None),
            "page": _to_int(row.get("page")),
            "confidence": _to_float(row.get("confidence")),
        }
        records.append(rec)
    return records
