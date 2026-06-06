"""FIE API: natural-language financial Q&A over delivered workbooks (Phase 5.2).

POST /api/fie/answer  { query, company?, audience? } -> structured Response.

Stores are lazy-loaded from storage/outputs and cached. External sources (PSX/
News/forecast) are not wired by default — answers run on internal data unless an
operator configures adapters; the engine degrades gracefully (architecture §0.3).
"""

from __future__ import annotations

import os
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import STORAGE_ROOT
from app.engines.fie import ExternalSources, FinancialFactStore, FinancialIntelligenceEngine

router = APIRouter(prefix="/fie", tags=["fie"])

_OUTPUTS = os.path.join(str(STORAGE_ROOT), "outputs")

# default company -> workbook file (extend as more are delivered)
_WORKBOOKS = {
    "Millat Tractors Limited": "millat_filled_fixed.xlsx",
    "Lucky Cement Limited": "lucky_filled_fixed.xlsx",
}
_DEFAULT_COMPANY = "Millat Tractors Limited"


class AnswerRequest(BaseModel):
    query: str
    company: str | None = None
    audience: str = "analyst"


@lru_cache(maxsize=8)
def _store(company: str) -> FinancialFactStore:
    fname = _WORKBOOKS.get(company)
    if fname is None:
        raise KeyError(company)
    path = os.path.join(_OUTPUTS, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return FinancialFactStore.from_workbook(path)


def _engine(company: str) -> FinancialIntelligenceEngine:
    primary = _store(company)
    # register the other delivered workbooks as peers (for peer_comparison)
    peers = {}
    for name in _WORKBOOKS:
        if name != company:
            try:
                peers[name] = _store(name)
            except (KeyError, FileNotFoundError):
                pass
    return FinancialIntelligenceEngine(primary, external=ExternalSources(peers=peers))


@router.post("/answer")
def answer(req: AnswerRequest) -> dict:
    company = req.company or _DEFAULT_COMPANY
    try:
        engine = _engine(company)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"No workbook for company {company!r}")
    resp = engine.answer(req.query, audience=req.audience)
    return resp.model_dump()


@router.get("/companies")
def companies() -> dict:
    available = [c for c, f in _WORKBOOKS.items()
                 if os.path.exists(os.path.join(_OUTPUTS, f))]
    return {"companies": available, "default": _DEFAULT_COMPANY}
