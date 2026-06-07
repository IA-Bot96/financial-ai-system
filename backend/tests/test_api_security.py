"""HTTP-layer security: security headers, request input validation (query length /
empty), generic error handler, and the rate-limit middleware (429)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import BodySizeLimitMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from app.core.security import TokenBucket
from app.main import app

client = TestClient(app)


def test_body_size_limit_exempts_upload_paths():
    """Large JSON requests are rejected (413), but multipart upload paths are exempt
    (they enforce their own per-file limits) — the bug that 413'd workbook uploads."""
    sub = FastAPI()

    @sub.post('/api/other')
    def other():
        return {'ok': True}

    @sub.post('/api/fie/sessions')
    def sessions():
        return {'ok': True}

    sub.add_middleware(BodySizeLimitMiddleware, max_bytes=10,
                       exempt_prefixes=('/api/fie/sessions', '/api/extraction/jobs'))
    c = TestClient(sub)
    big = b'x' * 100  # > max_bytes=10
    assert c.post('/api/other', content=big).status_code == 413          # capped
    assert c.post('/api/fie/sessions', content=big).status_code == 200   # exempt


# --- security headers on every response ------------------------------------
def test_security_headers_present():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"


# --- request input validation (query length / empty) ----------------------
def test_query_too_long_is_422():
    r = client.post("/api/fie/answer", json={"query": "x" * 5000})
    assert r.status_code == 422


def test_empty_query_is_422():
    r = client.post("/api/fie/answer", json={"query": "   "})
    assert r.status_code == 422


def test_unknown_company_still_404_not_swallowed():
    # the generic 500 handler must not swallow a deliberate HTTPException
    r = client.post("/api/fie/answer",
                    json={"query": "revenue 2024", "company": "Nonexistent Co"})
    assert r.status_code == 404


# --- rate limit middleware (fresh app so we can enable + tighten it) -------
def test_rate_limit_returns_429_after_burst():
    sub = FastAPI()

    @sub.get("/api/fie/ping")
    def ping():
        return {"ok": True}

    sub.add_middleware(SecurityHeadersMiddleware, force_https=False)
    sub.add_middleware(RateLimitMiddleware,
                       bucket=TokenBucket(capacity=1, refill_seconds=999),
                       paths=("/api/fie",))
    c = TestClient(sub)
    assert c.get("/api/fie/ping").status_code == 200      # first token
    r = c.get("/api/fie/ping")                             # bucket empty
    assert r.status_code == 429 and r.headers.get("Retry-After") == "30"
