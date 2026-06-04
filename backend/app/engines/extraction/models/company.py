"""Company-level result after multi-year resolution across reports."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.financials import FinancialTable
from app.engines.extraction.models.insight import Insight


class CompanyResult(BaseModel):
    company: Optional[str] = None
    fiscal_years: list[int] = Field(default_factory=list, description="All data years, ascending")
    source_reports: list[str] = Field(default_factory=list)
    tables: list[FinancialTable] = Field(default_factory=list, description="Merged multi-year tables")
    insights: list[Insight] = Field(default_factory=list)
    insights_review: list[Insight] = Field(default_factory=list)
