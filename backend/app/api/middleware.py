"""HTTP middleware for security hardening (Tier-A).

Body-size limit, security headers (+ optional HSTS), per-IP token-bucket rate limit,
and a per-request wall-clock timeout. Registered in app.main; each is config-driven.
"""

from __future__ import annotations

import anyio
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.security import TokenBucket


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized requests up front via Content-Length (413)."""

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > self.max_bytes:
            return JSONResponse({"detail": "request entity too large"}, status_code=413)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard hardening headers; HSTS only when serving over HTTPS."""

    def __init__(self, app, *, force_https: bool = False) -> None:
        super().__init__(app)
        self.force_https = force_https

    async def dispatch(self, request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-XSS-Protection", "0")
        if self.force_https:
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-client-IP token-bucket limit on the expensive API paths."""

    def __init__(self, app, *, bucket: TokenBucket, paths: tuple[str, ...]) -> None:
        super().__init__(app)
        self.bucket = bucket
        self.paths = paths

    async def dispatch(self, request, call_next):
        if any(request.url.path.startswith(p) for p in self.paths):
            ip = request.client.host if request.client else "unknown"
            if not self.bucket.allow(ip):
                return JSONResponse(
                    {"detail": "rate limit exceeded; please retry shortly"},
                    status_code=429, headers={"Retry-After": "30"})
        return await call_next(request)


class TimeoutMiddleware(BaseHTTPMiddleware):
    """Return 504 if a request exceeds the wall-clock budget."""

    def __init__(self, app, *, seconds: float) -> None:
        super().__init__(app)
        self.seconds = seconds

    async def dispatch(self, request, call_next):
        try:
            with anyio.fail_after(self.seconds):
                return await call_next(request)
        except TimeoutError:
            return JSONResponse({"detail": "request timed out"}, status_code=504)
