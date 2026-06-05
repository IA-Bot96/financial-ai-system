"""Template mapping plan — the set of cell writes computed from extracted data."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.common import SourceRef


class CellWrite(BaseModel):
    sheet: str
    coordinate: str                 # e.g. "B5"
    year: Optional[int] = None
    value: Optional[float] = None
    template_label: str = ""        # the template's row label
    matched_label: str = ""         # the extracted line it matched
    confidence: float = 0.0
    source_report_year: Optional[int] = None
    source: Optional[SourceRef] = None   # value-level lineage (report/page/table)
    note: Optional[str] = None      # e.g. "withheld:metric_mismatch", "withheld:tieout"


class MappingPlan(BaseModel):
    """Auditable plan: every value placement + provenance, applied separately."""

    writes: list[CellWrite] = Field(default_factory=list)
    sheets_processed: list[str] = Field(default_factory=list)
    sheets_skipped: list[str] = Field(default_factory=list)
    # Computed/formula-heavy sheets (skipped from mapping, but they ARE the output
    # statements) — their formulas use the signed P&L/BS convention for validation.
    formula_sheets: list[str] = Field(default_factory=list)
    unmatched_template_labels: list[str] = Field(default_factory=list)
    # Validation gate: matches rejected because they contradicted the audited
    # face statements (kept for audit, NOT written to the workbook).
    withheld: list[CellWrite] = Field(default_factory=list)
