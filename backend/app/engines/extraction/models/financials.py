"""Structured financial-statement models produced by the interpretation stage."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.common import SourceRef, StatementType


class LineItemValue(BaseModel):
    year: Optional[int] = Field(None, description="Fiscal year this value belongs to")
    value: Optional[float] = Field(None, description="Parsed numeric value (negatives for '(...)')")
    raw: Optional[str] = Field(None, description="Original text as printed, e.g. '(1,234)'")
    source_report_year: Optional[int] = Field(
        None, description="Which report this value was sourced from (multi-year resolution)"
    )
    # Value-level provenance (carried through the multi-year merge for lineage).
    source: Optional[SourceRef] = Field(None, description="Report/page/table this value came from")


class LineItem(BaseModel):
    label: str = Field(..., description="Line item name, e.g. 'Total assets'")
    section: Optional[str] = Field(None, description="Sub-section header above this row (e.g. 'LOCAL SALES')")
    unit: Optional[str] = Field(None, description="e.g. 'USD thousands', '%'")
    note_ref: Optional[str] = Field(None, description="Footnote/note reference, if any")
    values: list[LineItemValue] = Field(default_factory=list, description="One per period/year")
    # --- Structural hints for semantic formula generation (no-template output) ---
    # These are best-effort; downstream generation still validates every formula
    # against the reported total (tie-out), so a wrong hint never yields a wrong
    # number — it just falls back to the reported value.
    role: Optional[Literal["leaf", "subtotal", "total", "section_header"]] = Field(
        None,
        description=(
            "Structural role of this row: 'leaf' = an input value line; "
            "'subtotal' = sums the leaf lines under one sub-heading; "
            "'total' = combines subtotals (and/or leaves) for the statement/section; "
            "'section_header' = a heading row with no value of its own."
        ),
    )
    components: Optional[list[str]] = Field(
        None,
        description=(
            "For a 'subtotal'/'total' row only: the exact labels of the line items "
            "this row is the sum of (e.g. Operating Profit -> ['Gross profit', "
            "'Distribution costs', 'Administrative expenses', 'Other income']). Use the "
            "labels exactly as they appear in this same table. Null for leaf rows."
        ),
    )
    is_contra: Optional[bool] = Field(
        None,
        description=(
            "True if this line is a deduction/cost printed as a POSITIVE magnitude "
            "that is SUBTRACTED in its parent total (e.g. a discount, expense, or tax "
            "shown without a minus sign). False/null if it adds normally or is already "
            "signed negative."
        ),
    )
    # Resolved against the canonical metric registry (None if no confident match).
    canonical_metric: Optional[str] = Field(None, description="Canonical metric key")
    canonical_category: Optional[str] = Field(None, description="Registry category")
    # Scoped-resolution outcome (P2). 'ok' = canonical trusted; 'no_confident_metric'
    # = kept but uncanonical; 'quarantined' = confidently incompatible -> excluded
    # from merge/output and recorded in the rejected-line audit.
    resolution: Optional[Literal["ok", "no_confident_metric", "quarantined"]] = Field(None)
    quarantine_reason: Optional[str] = Field(None)


class FinancialTable(BaseModel):
    statement_type: StatementType = StatementType.other
    title: str = ""
    currency: Optional[str] = None
    unit_scale: Optional[str] = Field(None, description="e.g. 'thousands', 'millions'")
    # None = unknown; True = consolidated; False = unconsolidated/separate.
    consolidated: Optional[bool] = None
    # Where this table sits in the report hierarchy (P3/#12). 'primary' = an audited
    # face statement (the source of truth); 'note' = a breakdown note; 'analytical'
    # = ratios/six-year-summary/percentage tables (never truth).
    table_role: Optional[Literal["primary", "note", "analytical"]] = Field(None)
    years: list[int] = Field(default_factory=list)
    line_items: list[LineItem] = Field(default_factory=list)
    source: Optional[SourceRef] = None


class FinancialTableList(BaseModel):
    """Wrapper so GPT page-extraction returns a JSON object (one page can hold
    several tables)."""

    tables: list[FinancialTable] = Field(default_factory=list)
