"""Company-level result after multi-year resolution across reports."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.common import SourceRef, StatementType
from app.engines.extraction.models.financials import FinancialTable
from app.engines.extraction.models.insight import Insight


class RejectedLine(BaseModel):
    """A line excluded from output because it was confidently incompatible with its
    home statement (P2 quarantine) — kept for audit, never shipped."""
    label: str
    statement_type: Optional[StatementType] = None
    canonical_metric: Optional[str] = None
    canonical_category: Optional[str] = None
    reason: str = ""
    source: Optional[SourceRef] = None


class CompanyResult(BaseModel):
    company: Optional[str] = None
    fiscal_years: list[int] = Field(default_factory=list, description="All data years, ascending")
    source_reports: list[str] = Field(default_factory=list)
    tables: list[FinancialTable] = Field(default_factory=list, description="Merged multi-year tables")
    insights: list[Insight] = Field(default_factory=list)
    insights_review: list[Insight] = Field(default_factory=list)
    # P2: lines quarantined as confidently statement-incompatible (audit only).
    rejected_lines: list[RejectedLine] = Field(default_factory=list)
