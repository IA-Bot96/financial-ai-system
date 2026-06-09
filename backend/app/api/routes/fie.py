"""FIE API: natural-language financial Q&A over delivered workbooks (Phase 5.2).

POST /api/fie/answer  { query, company?, audience? } -> structured Response.

Stores are lazy-loaded from storage/outputs and cached. External sources (PSX/
News/forecast) are not wired by default — answers run on internal data unless an
operator configures adapters; the engine degrades gracefully (architecture §0.3).
"""

from __future__ import annotations

import logging
import os
import time
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.core.config import STORAGE_ROOT, get_settings
from app.core.metrics import METRICS
from app.engines.fie import ExternalSources, FinancialFactStore, FinancialIntelligenceEngine
from app.engines.fie.apis import ApiClient, CompanyOverview, HttpTransport, News, Symbols
from app.engines.fie.llm import NullLLM, OpenAILLM
from app.engines.fie.trace import TraceStore

_MAX_QUERY = get_settings().fie_max_query_chars
_MAX_AUDIENCE = 32
_log = logging.getLogger("app.api.fie")

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


@lru_cache(maxsize=1)
def _llm():
    """Singleton LLM client — NullLLM if no API key is configured."""
    s = get_settings()
    if not (s.openai_api_key or "").strip():
        _log.warning("OPENAI_API_KEY not set — LLM validation disabled (NullLLM)",
                     extra={"component": "fie-api"})
        return NullLLM()
    return OpenAILLM(
        model=s.openai_model,
        api_key=s.openai_api_key,
        max_input_chars=s.llm_max_input_chars,
        max_output_tokens=s.llm_max_output_tokens,
        json_temperature=s.llm_json_temperature,
        text_temperature=s.llm_text_temperature,
        seed=s.llm_seed,
    )


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
    _symbols = Symbols(client)
    external = ExternalSources(peers=peers, news=News(client), symbols=_symbols,
                               company_overview=CompanyOverview(client, symbols=_symbols))
    return FinancialIntelligenceEngine(primary, llm=_llm(), external=external)


@router.post("/answer")
def answer(req: AnswerRequest) -> dict:
    company = req.company or _DEFAULT_COMPANY
    settings = get_settings()
    try:
        engine = _engine(company)
    except (KeyError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"No workbook for company {company!r}")
    # cap external-adapter fan-out for this request (cost / abuse guard)
    _external_client().begin_request(settings.max_external_calls_per_request)

    # capture the full reasoning chain; persist it as an audit trail when enabled
    t0 = time.monotonic()
    resp, trace = engine.answer_with_trace(req.query, audience=req.audience)
    elapsed = time.monotonic() - t0
    cov = resp.coverage or {}
    band = resp.confidence.band if resp.confidence else "n/a"

    # metrics: volume by intent + outcome rates + latency
    METRICS.inc("fie_queries_total", intent=trace.frame.intent, confidence=band)
    METRICS.observe_latency("fie_answer_seconds", elapsed, intent=trace.frame.intent)
    if cov.get("degraded"):
        METRICS.inc("fie_degraded_total", intent=trace.frame.intent)
    if cov.get("dropped_claims"):
        METRICS.inc("fie_claims_dropped_total", cov["dropped_claims"])
    if cov.get("insufficient_evidence"):
        METRICS.inc("fie_insufficient_total", intent=trace.frame.intent)

    _log.info("fie answer trace=%s intent=%s conf=%s degraded=%s dropped_claims=%s %.0fms",
              trace.trace_id, trace.frame.intent, band,
              cov.get("degraded"), cov.get("dropped_claims", 0), elapsed * 1000,
              extra={"component": "fie-api"})
    if settings.fie_trace_enabled:
        try:
            TraceStore(settings.fie_trace_dir).persist(trace)
        except Exception:  # never let audit persistence break the response
            _log.warning("trace persist failed for %s", trace.trace_id,
                         extra={"component": "fie-api"})
    return resp.model_dump()


@router.get("/companies")
def companies() -> dict:
    available = [c for c, f in _WORKBOOKS.items()
                 if os.path.exists(os.path.join(_OUTPUTS, f))]
    return {"companies": available, "default": _DEFAULT_COMPANY}
