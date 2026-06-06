"""Operability: /readiness, /liveness, /metrics, secret-presence reporting, and the
shared rate-limit backend factory (Redis-optional, in-process fallback)."""

from fastapi.testclient import TestClient

from app.core import metrics as M
from app.core.config import secrets_status
from app.core.ratelimit import RedisFixedWindowLimiter, make_rate_limiter
from app.core.security import TokenBucket
from app.main import app

client = TestClient(app)


# --- health/liveness/readiness ---------------------------------------------
def test_liveness_and_health():
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/liveness").json()["status"] == "alive"


def test_readiness_reports_workbooks_and_secret_presence():
    r = client.get("/readiness")
    body = r.json()
    assert r.status_code in (200, 503)
    assert "workbooks" in body and "rate_limiter" in body
    # secrets are presence-only booleans — never values
    assert isinstance(body["secrets"], dict)
    assert all(isinstance(v, bool) for v in body["secrets"].values())


def test_secrets_status_is_presence_only():
    st = secrets_status()
    assert "openai" in st and all(isinstance(v, bool) for v in st.values())


# --- /metrics ---------------------------------------------------------------
def test_metrics_render_prometheus_text():
    M.METRICS.inc("test_counter_total", intent="ratio_analysis")
    M.METRICS.observe_latency("test_latency_seconds", 0.5, intent="ratio_analysis")
    r = client.get("/metrics")
    assert r.status_code == 200 and "text/plain" in r.headers["content-type"]
    body = r.text
    assert 'test_counter_total{intent="ratio_analysis"}' in body
    assert "test_latency_seconds_sum" in body and "test_latency_seconds_count" in body


def test_metrics_counter_accumulates():
    m = M.Metrics()
    m.inc("c_total", n=1)
    m.inc("c_total", n=1)
    m.inc("c_total", n=1)
    assert "c_total{n=\"1\"} 3.0" in m.render() or "c_total{n=\"1\"} 3" in m.render()


# --- rate-limit backend factory --------------------------------------------
def test_factory_returns_inprocess_without_redis_url():
    class _S:
        redis_url = ""
        rate_limit_capacity = 3
        rate_limit_refill_seconds = 1.0
    assert isinstance(make_rate_limiter(_S()), TokenBucket)


class _FakeRedis:
    """Subset of the redis client used by RedisFixedWindowLimiter."""
    def __init__(self):
        self.store: dict[str, int] = {}
    def incr(self, k):
        self.store[k] = self.store.get(k, 0) + 1
        return self.store[k]
    def expire(self, k, ttl):
        return True


def test_redis_fixed_window_allows_up_to_limit_then_blocks():
    lim = RedisFixedWindowLimiter(_FakeRedis(), limit=3, window_seconds=10)
    assert [lim.allow("ip") for _ in range(4)] == [True, True, True, False]
    assert lim.allow("other") is True            # per-key isolation


def test_redis_limiter_fails_open_on_backend_error():
    class _Boom:
        def incr(self, k): raise RuntimeError("redis down")
        def expire(self, k, ttl): ...
    lim = RedisFixedWindowLimiter(_Boom(), limit=1, window_seconds=10)
    assert lim.allow("ip") is True               # infra error must not block traffic
