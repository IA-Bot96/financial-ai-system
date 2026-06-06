"""FastAPI entrypoint."""
import logging
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from app.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    TimeoutMiddleware,
)
from app.api.routes import extraction, fie
from app.core.config import STORAGE_ROOT, get_settings, secrets_status
from app.core.logging import configure_logging
from app.core.metrics import METRICS
from app.core.ratelimit import make_rate_limiter
from app.core.security import RedactingFilter
from app.engines.fie import bootcheck

settings = get_settings()
configure_logging(settings.debug)
# Scrub secret-like substrings (API keys/tokens) from every log line.
logging.getLogger().addFilter(RedactingFilter())

_log = logging.getLogger("app.api")

# Fail-fast contract-integrity check: a mis-wired formula / authority matrix /
# taxonomy / citation contract aborts startup loudly instead of serving wrong answers.
_results = bootcheck.assert_contracts()
logging.getLogger("app.engines.fie").info(
    "FIE contract integrity OK (%d checks)", len(_results),
    extra={"component": "bootcheck"})
# Startup visibility: which secrets are configured (presence only, never values).
_log.info("secrets configured: %s", secrets_status(settings), extra={"component": "startup"})

app = FastAPI(title=settings.app_name)

# --- middleware (outermost first; add order is reverse of execution) -------
app.add_middleware(SecurityHeadersMiddleware, force_https=settings.force_https)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(TimeoutMiddleware, seconds=settings.request_timeout_seconds)
_limiter = make_rate_limiter(settings) if settings.rate_limit_enabled else None
_limiter_kind = type(_limiter).__name__ if _limiter else "disabled"
if _limiter is not None:
    app.add_middleware(RateLimitMiddleware, bucket=_limiter,
                       paths=("/api/fie", "/api/extraction"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list(),       # pinned origins (config-driven)
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(extraction.router, prefix="/api")
app.include_router(fie.router, prefix="/api")


# --- error handling: never leak internals; correlate with a request id -----
@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    rid = uuid.uuid4().hex[:12]
    METRICS.inc("app_unhandled_errors_total")
    _log.exception("unhandled error rid=%s path=%s", rid, request.url.path,
                   extra={"component": "api"})
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error", "request_id": rid},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/liveness")
def liveness() -> dict:
    """Process is up (no dependency checks)."""
    return {"status": "alive"}


@app.get("/readiness")
def readiness():
    """Ready to serve: at least one delivered workbook present + boot contracts OK.
    Reports secret presence (never values) and the active rate-limiter backend."""
    outputs = os.path.join(str(STORAGE_ROOT), "outputs")
    workbooks = ([f for f in os.listdir(outputs) if f.endswith(".xlsx")]
                 if os.path.isdir(outputs) else [])
    ready = bool(workbooks)
    body = {
        "status": "ready" if ready else "not_ready",
        "workbooks": len(workbooks),
        "contracts_ok": True,                  # asserted at boot, else startup aborts
        "rate_limiter": _limiter_kind,
        "secrets": secrets_status(settings),   # {name: bool}
    }
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/metrics")
def metrics() -> PlainTextResponse:
    """Prometheus text exposition of in-process counters/latency."""
    return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4")
