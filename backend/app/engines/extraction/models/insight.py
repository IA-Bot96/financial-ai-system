"""Insight model — fields map 1:1 to the output Insights sheet columns:

    Year | Source Report Year | Area | Takeaway | Source Section | Page | Confidence
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# Column order for the Insights worksheet (used by the output layer).
INSIGHT_COLUMNS: list[str] = [
    "Year",
    "Source Report Year",
    "Area",
    "Takeaway",
    "Source Section",
    "Page",
    "Confidence",
]


class Insight(BaseModel):
    year: Optional[int] = Field(None, description="Fiscal year the insight relates to")
    source_report_year: Optional[int] = Field(None, description="Year of the report it came from")
    area: str = Field(..., description="Theme, e.g. 'Growth and market expansion', 'Risks', 'Outlook'")
    takeaway: str = Field(..., description="Concise, factual insight statement")
    source_section: Optional[str] = Field(None, description="e.g. 'CEO Review', 'MD&A', 'Outlook'")
    page: Optional[int] = Field(None, description="1-based page number")
    confidence: float = Field(0.0, ge=0.0, le=1.0)

    def to_row(self) -> list:
        """Return values in INSIGHT_COLUMNS order for the Excel sheet."""
        return [
            self.year,
            self.source_report_year,
            self.area,
            self.takeaway,
            self.source_section,
            self.page,
            self.confidence,
        ]


class InsightList(BaseModel):
    """Wrapper so GPT returns a JSON object (not a bare array)."""

    insights: list[Insight] = Field(default_factory=list)
