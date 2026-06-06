"""Reasoning trace & replay (L9 / Phase 5.1).

Every answer can emit a TraceRecord capturing the full reasoning chain — frame,
plan, evidence, and the rendered Response — so a reviewer can reconstruct exactly
which cells, reports, and external responses produced the answer (architecture §11).
"""

from __future__ import annotations

import json
import os
from typing import Optional

from pydantic import BaseModel, Field

from .models import (
    EvidenceItem,
    QueryFrame,
    Response,
    SourcePlan,
)


class TraceRecord(BaseModel):
    trace_id: str
    query: str
    audience: str = "analyst"
    company: Optional[str] = None
    frame: QueryFrame
    plan: SourcePlan
    evidence: list[EvidenceItem] = Field(default_factory=list)
    response: Response


class TraceStore:
    """Persist/load TraceRecords as JSON (one file per trace)."""

    def __init__(self, directory: str) -> None:
        self.directory = directory

    def persist(self, record: TraceRecord) -> str:
        os.makedirs(self.directory, exist_ok=True)
        path = os.path.join(self.directory, f"{record.trace_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record.model_dump(), fh, indent=2, default=str)
        return path

    def load(self, trace_id: str) -> TraceRecord:
        path = os.path.join(self.directory, f"{trace_id}.json")
        with open(path, encoding="utf-8") as fh:
            return TraceRecord.model_validate(json.load(fh))
