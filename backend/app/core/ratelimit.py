"""Rate-limit backend selection (shared vs in-process).

The in-memory ``security.TokenBucket`` is correct for a single process, but a
multi-instance deploy (e.g. several Heroku dynos) needs a SHARED store or each
instance enforces the limit independently. This module provides a Redis-backed
fixed-window limiter and a factory that picks it when ``REDIS_URL`` is configured,
falling back to the in-process token bucket otherwise.

Both backends expose the same ``allow(key) -> bool`` surface the middleware uses.
Redis is imported lazily so it is never a hard dependency in dev/test.
"""

from __future__ import annotations

import logging

from app.core.security import TokenBucket

_log = logging.getLogger("app.core.ratelimit")


class RedisFixedWindowLimiter:
    """Shared fixed-window limiter: at most ``limit`` requests per ``window`` seconds
    per key, across all instances. Atomic via INCR (+ EXPIRE on the first hit).

    Fixed-window is a deliberate simplification of the in-process token bucket — it
    is the standard, atomically-correct shared primitive. ``limit``/``window`` are
    derived from the same capacity/refill knobs so behavior is comparable
    (capacity requests per `capacity * refill_seconds` window ≈ 1 / refill_seconds)."""

    def __init__(self, client, *, limit: int, window_seconds: int,
                 prefix: str = "rl:") -> None:
        self.client = client
        self.limit = max(1, limit)
        self.window = max(1, int(window_seconds))
        self.prefix = prefix

    def allow(self, key: str) -> bool:
        rkey = f"{self.prefix}{key}"
        try:
            n = self.client.incr(rkey)
            if n == 1:
                self.client.expire(rkey, self.window)
            return n <= self.limit
        except Exception:  # Redis hiccup -> fail OPEN (don't block traffic on infra)
            _log.warning("rate-limit backend error; allowing request", exc_info=False,
                         extra={"component": "ratelimit"})
            return True


def make_rate_limiter(settings):
    """Return a shared Redis limiter when REDIS_URL is set and redis is importable,
    else the in-process token bucket. Falls back safely on any connection error."""
    url = getattr(settings, "redis_url", "") or ""
    capacity = settings.rate_limit_capacity
    refill = settings.rate_limit_refill_seconds
    if url:
        try:
            import redis  # lazy: never required in dev/test
            client = redis.Redis.from_url(url, socket_timeout=0.25,
                                          socket_connect_timeout=0.25)
            client.ping()
            window = max(1, int(capacity * refill))
            _log.info("rate limiter: Redis (limit=%d / %ds window)", capacity, window,
                      extra={"component": "ratelimit"})
            return RedisFixedWindowLimiter(client, limit=capacity, window_seconds=window)
        except Exception as exc:  # noqa: BLE001
            _log.warning("REDIS_URL set but unusable (%s); using in-process limiter", exc,
                         extra={"component": "ratelimit"})
    return TokenBucket(capacity=capacity, refill_seconds=refill)
