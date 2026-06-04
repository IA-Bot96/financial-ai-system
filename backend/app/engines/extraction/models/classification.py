"""Schemas for batched GPT classification of ambiguous tables."""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.engines.extraction.models.common import StatementType


class TableClassification(BaseModel):
    table_id: str
    statement_type: StatementType


class BatchClassification(BaseModel):
    classifications: list[TableClassification] = Field(default_factory=list)
