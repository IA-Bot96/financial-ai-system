"""FIE workbook sessions (API layer only — no engine changes).

The desktop app opens *arbitrary* user workbooks, so we can't use the hardcoded
company->file map of /api/fie/answer. A session ingests one uploaded workbook ONCE into
a resident in-memory store + engine, keyed by ``session_id``; every query reuses that
resident store. The expensive parse (``FinancialFactStore.from_workbook``) runs only on
create and reload — NEVER on the query path (see test_sessions.py for the regression guard).

    POST   /api/fie/sessions               (multipart .xlsx)        -> {session_id, company, years, sheets, metrics}
    POST   /api/fie/sessions/{id}/answer   {query, audience?}       -> FIE Response
    POST   /api/fie/sessions/{id}/reload   (multipart .xlsx)        -> re-ingest + replace store (cache-bust)
    DELETE /api/fie/sessions/{id}                                   -> drop the session
    GET    /api/fie/sessions/{id}                                   -> session metadata

This module only *uses* the engine (FinancialFactStore / FinancialIntelligenceEngine) and
shared helpers; it does not modify app/engines/*.
"""

from __future__ import annotations

import logging
import os
import time
import uuid

import openpyxl
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, field_validator

from app.api.routes.fie import _external_client, _llm    # shared resilient HTTP client + LLM
from app.core.config import STORAGE_ROOT, get_settings
from app.core.metrics import METRICS
from app.core.security import UploadRejected, assert_safe_upload
from app.engines.fie import (ExternalSources, FinancialFactStore,
                             FinancialIntelligenceEngine)
from app.engines.fie.apis import News, RegistryFetcher, Symbols
from app.engines.fie.ingest.classify import classify_sheet
from app.engines.fie.trace import TraceStore

router = APIRouter(prefix="/fie/sessions", tags=["fie-sessions"])
_log = logging.getLogger("app.api.fie")

_MAX_QUERY = get_settings().fie_max_query_chars
_EDITABLE_ROLES = {"statement", "detail"}   # financial sheets; meta-sheets are read-only
_SESSIONS_DIR = os.path.join(str(STORAGE_ROOT), "sessions")

# session_id -> {engine, store, meta, path}. The store is held RESIDENT in memory; the
# query path looks it up and never re-parses the workbook.
_SESSIONS: dict[str, dict] = {}


class HistoryTurn(BaseModel):
    role: str                  # "user" | "assistant"
    text: str
    frame: dict | None = None  # resolved QueryFrame echoed back from the prior response


class SessionAnswerRequest(BaseModel):
    query: str
    audience: str = "analyst"
    history: list[HistoryTurn] = []   # recent conversation turns for follow-up context

    @field_validator("query")
    @classmethod
    def _bounded_query(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("query must not be empty")
        if len(v) > _MAX_QUERY:
            raise ValueError(f"query too long (max {_MAX_QUERY} chars)")
        return v

    @field_validator("audience")
    @classmethod
    def _bounded_audience(cls, v: str) -> str:
        if v and len(v) > 32:
            raise ValueError("audience too long")
        return v


def _sheet_meta(path: str) -> list[dict]:
    """Per-sheet {name, role, editable} from the workbook (classified at the API layer)."""
    out: list[dict] = []
    wb = openpyxl.load_workbook(path, read_only=True)
    try:
        for name in wb.sheetnames:
            role = classify_sheet(name)
            out.append({"name": name, "role": role, "editable": role in _EDITABLE_ROLES})
    finally:
        wb.close()
    return out


async def _read_xlsx(f: UploadFile) -> bytes:
    data = await f.read()
    try:
        assert_safe_upload(f.filename or "workbook.xlsx", data,
                           max_bytes=get_settings().max_excel_upload_bytes, kinds=("xlsx",))
    except UploadRejected as e:
        raise HTTPException(status_code=400, detail=f"Rejected {f.filename!r}: {e}")
    return data


def _build(session_id: str, data: bytes) -> dict:
    """Persist bytes, ingest ONCE (from_workbook), build a resident engine, return record."""
    os.makedirs(_SESSIONS_DIR, exist_ok=True)
    path = os.path.join(_SESSIONS_DIR, f"{session_id}.xlsx")
    with open(path, "wb") as fh:
        fh.write(data)
    store = FinancialFactStore.from_workbook(path)          # the only parse (create/reload)
    client = _external_client()
    symbols = Symbols(client)
    # RegistryFetcher makes the full 17-API PSX catalog callable generically; the planner's
    # query-driven shortlist (plan.registry_apis) decides which subset actually fires.
    external = ExternalSources(
        news=News(client), symbols=symbols,
        registry_fetcher=RegistryFetcher(client),
    )
    engine = FinancialIntelligenceEngine(store, llm=_llm(), external=external)
    meta = {"session_id": session_id, "company": store.company, "years": store.years,
            "sheets": _sheet_meta(path), "metrics": sorted(store.available_metrics())}
    return {"engine": engine, "store": store, "meta": meta, "path": path}


def _get(session_id: str) -> dict:
    rec = _SESSIONS.get(session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
    return rec


@router.post("")
async def create_session(file: UploadFile = File(...)) -> dict:
    data = await _read_xlsx(file)
    sid = uuid.uuid4().hex[:16]
    _SESSIONS[sid] = _build(sid, data)
    _log.info("fie session created %s company=%s years=%s",
              sid, _SESSIONS[sid]["meta"]["company"], _SESSIONS[sid]["meta"]["years"],
              extra={"component": "fie-api"})
    return _SESSIONS[sid]["meta"]


@router.get("/{session_id}")
def get_session(session_id: str) -> dict:
    return _get(session_id)["meta"]


@router.get("/{session_id}/series")
def session_series(session_id: str) -> dict:
    """Headline metric values per year for the dashboard (read-only over the resident
    store; the query path does NOT re-parse the workbook). Returns
    {years, series:{metric:{year:value|None}}}. Ratios are derived client-side."""
    store = _get(session_id)["store"]
    years = store.years
    series: dict[str, dict[int, float | None]] = {}
    for metric in sorted(store.available_metrics()):
        row: dict[int, float | None] = {}
        for y in years:
            try:
                row[y] = store.lookup(metric, y).value
            except KeyError:
                row[y] = None
        series[metric] = row

    # A few dashboard inputs live at the detail level (not headline): depreciation must be
    # summed across its line items per year (for EBITDA = operating profit + depreciation);
    # EPS is a single per-year figure. Surface them so the client can derive EBITDA / EPS.
    detail = store.detail()
    for metric, agg in (("depreciation_expense", "sum"), ("earnings_per_share", "first")):
        if metric in series:
            continue
        sub = detail[detail["metric"] == metric]
        if sub.empty:
            continue
        row = {}
        for y in years:
            vals = sub[sub["year"] == y]["value"].dropna()
            if vals.empty:
                row[y] = None
            else:
                row[y] = float(vals.sum()) if agg == "sum" else float(vals.iloc[0])
        if any(v is not None for v in row.values()):
            series[metric] = row

    return {"years": years, "series": series}


@router.post("/{session_id}/answer")
def answer_in_session(session_id: str, req: SessionAnswerRequest) -> dict:
    rec = _get(session_id)
    engine = rec["engine"]                                  # RESIDENT engine — no re-ingest
    settings = get_settings()
    _external_client().begin_request(settings.max_external_calls_per_request)

    # History is owned by the frontend. Assistant turns carry the resolved QueryFrame
    # (echoed back in every response) instead of prose text — GPT gets compact, structured
    # context for follow-up resolution rather than verbose financial narratives.
    history = [{"role": t.role, "text": t.text, "frame": t.frame} for t in req.history]
    _log.info("fie session=%s query=%r history_len=%d audience=%s",
              session_id, req.query, len(history), req.audience,
              extra={"component": "fie-api"})

    t0 = time.monotonic()
    resp, trace = engine.answer_with_trace(req.query, audience=req.audience, history=history)
    elapsed = time.monotonic() - t0
    cov = resp.coverage or {}
    band = resp.confidence.band if resp.confidence else "n/a"

    METRICS.inc("fie_queries_total", intent=trace.frame.intent, confidence=band)
    METRICS.observe_latency("fie_answer_seconds", elapsed, intent=trace.frame.intent)
    if cov.get("degraded"):
        METRICS.inc("fie_degraded_total", intent=trace.frame.intent)
    if cov.get("dropped_claims"):
        METRICS.inc("fie_claims_dropped_total", cov["dropped_claims"])
    if cov.get("insufficient_evidence"):
        METRICS.inc("fie_insufficient_total", intent=trace.frame.intent)
    _log.info("fie session=%s trace=%s intent=%s conf=%s degraded=%s %.0fms",
              session_id, trace.trace_id, trace.frame.intent, band,
              cov.get("degraded"), elapsed * 1000, extra={"component": "fie-api"})

    if settings.fie_trace_enabled:
        try:
            TraceStore(settings.fie_trace_dir).persist(trace)
        except Exception:
            _log.warning("trace persist failed for %s", trace.trace_id,
                         extra={"component": "fie-api"})

    return {**resp.model_dump(), "frame": trace.frame.model_dump()}


@router.post("/{session_id}/reload")
async def reload_session(session_id: str, file: UploadFile = File(...)) -> dict:
    _get(session_id)                                        # 404 if unknown
    data = await _read_xlsx(file)
    _SESSIONS[session_id] = _build(session_id, data)        # replace resident store (cache-bust)
    _log.info("fie session reloaded %s", session_id, extra={"component": "fie-api"})
    return _SESSIONS[session_id]["meta"]


@router.delete("/{session_id}")
def delete_session(session_id: str) -> dict:
    rec = _SESSIONS.pop(session_id, None)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"unknown session {session_id!r}")
    try:
        os.remove(rec["path"])
    except OSError:
        pass
    return {"deleted": session_id}
