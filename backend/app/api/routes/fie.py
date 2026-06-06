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
from pydantic import BaseModel, field_validator

from app.core.config import STORAGE_ROOT, get_settings
from app.engines.fie import ExternalSources, FinancialFactStore, FinancialIntelligenceEngine
from app.engines.fie.apis import ApiClient, HttpTransport, News, Symbols

_MAX_QUERY = get_settings().fie_max_query_chars
_MAX_AUDIENCE = 32

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

    @field_validator("query")
    @classmethod
    def _bounded_query(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("query must not be empty")
        if len(v) > _MAX_QUERY:
            raise ValueError(f"query too long (max {_MAX_QUERY} chars)")
        return v

    @field_validator("company", "audience")
    @classmethod
    def _bounded_field(cls, v):
        if v is not None and len(v) > 128:
            raise ValueError("field too long")
        return v


@lru_cache(maxsize=8)
def _store(company: str) -> FinancialFactStore:
    fname = _WORKBOOKS.get(company)
    if fname is None:
        raise KeyError(company)
    path = os.path.join(_OUTPUTS, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return FinancialFactStore.from_workbook(path)


@lru_cache(maxsize=1)
def _external_client() -> ApiClient:
    """Shared resilient client (retry/breaker/cache) for the live HTTP transport."""
    return ApiClient(HttpTransport())


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
    # News failover search + symbol resolution (reads provider keys from .env;
    # with no keys configured the engine simply degrades — no news evidence).
    client = _external_client()
    external = ExternalSources(peers=peers, news=News(client), symbols=Symbols(client))
    return FinancialIntelligenceEngine(primary, external=external)


@router.post("/answer")
def answer(req: AnswerRequest) -> dict:
    company = req.company or _DEFAULT_COMPANY
    try:
        engine = _engine(company)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"No workbook for company {company!r}")
    # cap external-adapter fan-out for this request (cost / abuse guard)
    _external_client().begin_request(get_settings().max_external_calls_per_request)
    resp = engine.answer(req.query, audience=req.audience)
    return resp.model_dump()


@router.get("/companies")
def companies() -> dict:
    available = [c for c, f in _WORKBOOKS.items()
                 if os.path.exists(os.path.join(_OUTPUTS, f))]
    return {"companies": available, "default": _DEFAULT_COMPANY}
