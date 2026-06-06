"""ApiClient security guards: SSRF path-param validation + per-request external-call
budget. Both fail closed (a failed CallResult), never make the request."""

from app.engines.fie.apis.base import ApiClient, ApiSpec


class _CountingT:
    def __init__(self):
        self.calls = 0
        self.last_url = None
    def get(self, url, params, timeout):
        self.calls += 1
        self.last_url = url
        return {"ok": True}
    def post(self, url, body, timeout, content_type="json"):
        raise AssertionError("GET only")


def _spec():
    return ApiSpec(id="T.Sym", base_url="https://x.test", path="company/{symbol}",
                   method="GET", response_type="json",
                   normalizer=lambda raw, p, s, t: [])


def _client(t):
    return ApiClient(t, sleep=lambda s: None, now=lambda: "t")


def test_ssrf_unsafe_path_param_is_blocked():
    t = _CountingT()
    c = _client(t)
    for bad in ("MTL/../admin", "a:b", "x?y", "http://evil", "a b"):
        res = c.call(_spec(), symbol=bad)
        assert res.status == "failed" and "unsafe path param" in res.note
    assert t.calls == 0                         # never hit the network


def test_safe_path_param_passes_through():
    t = _CountingT()
    res = _client(t).call(_spec(), symbol="MTL")
    assert res.status == "ok" and t.calls == 1
    assert t.last_url == "https://x.test/company/MTL"


def test_external_call_budget_fails_closed_after_limit():
    t = _CountingT()
    c = _client(t)
    c.begin_request(2)                          # allow 2 external calls this "request"
    assert c.call(_spec(), symbol="MTL").status == "ok"
    assert c.call(_spec(), symbol="LUCK").status == "ok"
    third = c.call(_spec(), symbol="ENGRO")
    assert third.status == "failed" and "budget" in third.note
    assert t.calls == 2                         # the 3rd never reached the transport


def test_no_budget_set_is_unrestricted():
    t = _CountingT()
    c = _client(t)                              # begin_request never called
    for _ in range(5):
        assert c.call(_spec(), symbol="MTL").status == "ok"
    assert t.calls == 5
