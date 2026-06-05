"""Data models produced by the Table & Section detection layer (Layer 2)."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from app.engines.extraction.models.common import SourceRef, StatementType


class TableOrientation(str, Enum):
    vertical = "vertical"      # labels down the rows, periods across columns (canonical)
    horizontal = "horizontal"  # periods down the rows; transposed -> normalized to vertical
    unknown = "unknown"


class Section(BaseModel):
    statement_type: StatementType
    title: str
    start_page: int
    end_page: int
    # None = unspecified; True = consolidated; False = unconsolidated/separate.
    consolidated: Optional[bool] = None


class RawTable(BaseModel):
    """A detected table, normalized to canonical (labels in column 0, periods across)."""

    table_id: str
    statement_type: StatementType = StatementType.other
    title: str = ""
    header: list[str] = Field(default_factory=list, description="Canonical header row")
    rows: list[list[str]] = Field(default_factory=list, description="Data rows (header excluded)")
    orientation: TableOrientation = TableOrientation.unknown
    years: list[int] = Field(default_factory=list)
    currency: Optional[str] = None
    unit_scale: Optional[str] = None

    # Classification provenance
    needs_review: bool = Field(False, description="True -> let GPT classify in the interpretation stage")
    classification_method: str = ""
    classification_score: float = 0.0
    # None = unknown; True = consolidated; False = unconsolidated/separate.
    consolidated: Optional[bool] = None
    # True if reconstructed from a scanned/OCR page (grid is unreliable -> the
    # interpretation stage extracts those pages with GPT instead).
    from_ocr: bool = False

    source: Optional[SourceRef] = None

    @property
    def n_cols(self) -> int:
        return len(self.header) if self.header else (len(self.rows[0]) if self.rows else 0)


class TableSet(BaseModel):
    file_name: str
    report_year: Optional[int] = None
    sections: list[Section] = Field(default_factory=list)
    tables: list[RawTable] = Field(default_factory=list)
