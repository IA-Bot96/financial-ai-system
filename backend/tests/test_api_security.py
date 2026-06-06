"""HTTP-layer security: security headers, request input validation (query length /
empty), generic error handler, and the rate-limit middleware (429)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.core.security import TokenBucket
from app.main import app

client = TestClient(app)


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
