"""Combined Layer 3 output for one document."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.financials import FinancialTable
from app.engines.extraction.models.insight import Insight


class DocumentResult(BaseModel):
    file_name: str
    report_year: Optional[int] = None
    tables: list[FinancialTable] = Field(default_factory=list)
    insights: list[Insight] = Field(default_factory=list)          # confidence >= review
    insights_review: list[Insight] = Field(default_factory=list)   # review bucket
