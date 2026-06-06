"""FastAPI entrypoint."""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    TimeoutMiddleware,
)
from app.api.routes import extraction, fie
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.security import RedactingFilter, TokenBucket
from app.engines.fie import bootcheck

settings = get_settings()
configure_logging(settings.debug)
# Scrub secret-like substrings (API keys/tokens) from every log line.
logging.getLogger().addFilter(RedactingFilter())

# Fail-fast contract-integrity check: a mis-wired formula / authority matrix /
# taxonomy / citation contract aborts startup loudly instead of serving wrong answers.
_results = bootcheck.assert_contracts()
logging.getLogger("app.engines.fie").info(
    "FIE contract integrity OK (%d checks)", len(_results),
    extra={"component": "bootcheck"})

app = FastAPI(title=settings.app_name)

# --- middleware (outermost first; add order is reverse of execution) -------
app.add_middleware(SecurityHeadersMiddleware, force_https=settings.force_https)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
app.add_middleware(TimeoutMiddleware, seconds=settings.request_timeout_seconds)
if settings.rate_limit_enabled:
    app.add_middleware(
        RateLimitMiddleware,
        bucket=TokenBucket(capacity=settings.rate_limit_capacity,
                           refill_seconds=settings.rate_limit_refill_seconds),
        paths=("/api/fie", "/api/extraction"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list(),       # pinned origins (config-driven)
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(extraction.router, prefix="/api")
app.include_router(fie.router, prefix="/api")

_log = logging.getLogger("app.api")


# --- error handling: never leak internals; correlate with a request id -----
@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    rid = uuid.uuid4().hex[:12]
    _log.exception("unhandled error rid=%s path=%s", rid, request.url.path,
                   extra={"component": "api"})
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error", "request_id": rid},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
