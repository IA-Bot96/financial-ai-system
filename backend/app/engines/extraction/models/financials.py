"""Structured financial-statement models produced by Layer 3 (GPT)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.common import SourceRef, StatementType


class LineItemValue(BaseModel):
    year: Optional[int] = Field(None, description="Fiscal year this value belongs to")
    value: Optional[float] = Field(None, description="Parsed numeric value (negatives for '(...)')")
    raw: Optional[str] = Field(None, description="Original text as printed, e.g. '(1,234)'")
    source_report_year: Optional[int] = Field(
        None, description="Which report this value was sourced from (multi-year resolution)"
    )


class LineItem(BaseModel):
    label: str = Field(..., description="Line item name, e.g. 'Total assets'")
    unit: Optional[str] = Field(None, description="e.g. 'USD thousands', '%'")
    note_ref: Optional[str] = Field(None, description="Footnote/note reference, if any")
    values: list[LineItemValue] = Field(default_factory=list, description="One per period/year")
    # Resolved against the canonical metric registry (None if no confident match).
    canonical_metric: Optional[str] = Field(None, description="Canonical metric key")
    canonical_category: Optional[str] = Field(None, description="Registry category")


class FinancialTable(BaseModel):
    statement_type: StatementType = StatementType.other
    title: str = ""
    currency: Optional[str] = None
    unit_scale: Optional[str] = Field(None, description="e.g. 'thousands', 'millions'")
    years: list[int] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    source: Optional[SourceRef] = None
