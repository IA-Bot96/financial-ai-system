"""News layer: finance-native failover search across 8 free providers.

Offline via a fake transport. Covers: per-provider parsing into NewsArticle
(content + source), provider ordering, failover on limit/error/empty, no-key and
ticker-only skips, and query-relevance (symbol-scoped vs keyword)."""

from types import SimpleNamespace

from app.engines.fie import NewsArticle
from app.engines.fie.apis import ApiClient, News
from app.engines.fie.apis import news_providers as NP
from app.engines.fie.apis.news_providers import PROVIDERS


# --- offline transport -----------------------------------------------------
class FakeTransport:
    """Returns canned JSON by url-substring; raises (simulating 429) for fail_for."""
    def __init__(self, responses: dict, fail_for=()):
        self.responses = responses
        self.fail_for = tuple(fail_for)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        for sub in self.fail_for:
            if sub in url:
                raise RuntimeError("429 rate limited")
        for sub, body in self.responses.items():
            if sub in url:
                return body
        return {}                      # unknown endpoint -> empty -> parses to []

    def post(self, *a, **k):
        raise AssertionError("news providers are GET")


_KEYS = ("news_marketaux_key", "news_finnhub_key", "news_alphavantage_key",
         "news_newsdata_io_key", "news_newsapi_ai_key", "news_worldnewsapi_key",
         "news_gnews_io_key", "news_newsapi_org_key")


def _settings(**keys):
    ns = SimpleNamespace(news_max_articles=10)
    for k in _KEYS:
        setattr(ns, k, keys.get(k, ""))
    return ns


def _news(transport, **keys):
    client = ApiClient(transport, sleep=lambda s: None, now=lambda: "2026-06-06")
    return News(client, settings=_settings(**keys), providers=PROVIDERS)


_MARKETAUX = {"data": [{
    "title": "Millat Tractors posts record profit", "description": "FY result beat",
    "snippet": "Millat Tractors Limited reported...", "url": "https://x/mtl",
    "source": "Business Recorder", "published_at": "2026-06-05T09:00:00Z",
    "entities": [{"symbol": "MTL", "sentiment_score": 0.42}]}]}


# --- ordering --------------------------------------------------------------
def test_provider_order_is_finance_native():
    ids = [p.id for p in PROVIDERS]
    assert ids[:3] == ["marketaux", "finnhub", "alphavantage"]   # finance-native first
    assert ids[-1] == "newsapi_org"                              # delayed/non-commercial last
    # the two ticker-only feeds are flagged
    assert NP.PROVIDERS_BY_ID["finnhub"].requires_symbol
    assert NP.PROVIDERS_BY_ID["alphavantage"].requires_symbol


# --- marketaux parse + evidence (content + source preserved) ---------------
def test_marketaux_parse_stores_content_and_source():
    t = FakeTransport({"marketaux.com": _MARKETAUX})
    res = _news(t, news_marketaux_key="K").search("Millat", symbol="MTL")
    assert res.status == "ok" and len(res.items) == 1
    assert res.note.startswith("served_by=marketaux")
    it = res.items[0]
    assert it.claim == "Millat Tractors posts record profit" and it.kind == "external"
    loc = it.citations[0].locator
    assert loc["provider"] == "marketaux" and loc["source"] == "Business Recorder"
    assert loc["url"] == "https://x/mtl" and loc["link"] == "https://x/mtl" and loc["symbols"] == ["MTL"]
    assert loc["sentiment"] == 0.42 and loc["content"]            # content stored
    assert "author" in loc                                        # author key present (None here)
    assert it.freshness == "2026-06-05T09:00:00Z"


def test_query_relevance_symbol_vs_keyword():
    # symbol present -> scope to ticker (symbols=), no free-text search
    t = FakeTransport({"marketaux.com": _MARKETAUX})
    _news(t, news_marketaux_key="K").search("anything", symbol="MTL")
    p = t.calls[-1][1]
    assert p.get("symbols") == "MTL" and "search" not in p
    # no symbol -> free-text search on the query
    t2 = FakeTransport({"marketaux.com": _MARKETAUX})
    _news(t2, news_marketaux_key="K").search("cement demand outlook")
    p2 = t2.calls[-1][1]
    assert p2.get("search") == "cement demand outlook" and "symbols" not in p2


# --- failover --------------------------------------------------------------
def test_failover_when_top_provider_hits_limit():
    # marketaux raises (429); newsdata serves the result
    t = FakeTransport(
        {"newsdata.io": {"status": "success", "results": [
            {"title": "Sector update", "description": "d", "content": "body",
             "link": "https://n/1", "source_id": "dawn", "pubDate": "2026-06-04 10:00:00"}]}},
        fail_for=("marketaux.com",))
    res = _news(t, news_marketaux_key="K", news_newsdata_io_key="K2").search("cement")
    assert res.status == "ok" and res.note.startswith("served_by=newsdata")
    assert res.items[0].citations[0].locator["provider"] == "newsdata"
    tried = [u for u, _ in t.calls]
    assert any("marketaux.com" in u for u in tried)   # marketaux attempted first
    assert any("newsdata.io" in u for u in tried)     # then fell over to newsdata


def test_error_body_treated_as_empty_and_fails_over():
    # newsapi_org returns 200 with an error body -> parses to [] -> no provider left
    t = FakeTransport({"newsapi.org": {"status": "error", "code": "rateLimited"}})
    res = _news(t, news_newsapi_org_key="K").search("oil")
    assert res.status == "failed" and "tried=['newsapi_org']" in res.note


# --- skips -----------------------------------------------------------------
def test_ticker_only_feeds_skipped_on_keyword_query():
    # finnhub (ticker-only) has a key but no symbol -> skipped; gnews serves
    t = FakeTransport({"gnews.io": {"articles": [
        {"title": "Cement demand rises", "description": "d", "content": "c",
         "url": "https://g/1", "source": {"name": "GNews"}, "publishedAt": "2026-06-05"}]}})
    res = _news(t, news_finnhub_key="K", news_gnews_io_key="K2").search("cement", symbol=None)
    assert res.status == "ok" and res.note.startswith("served_by=gnews")
    assert not any("finnhub.io" in u for u, _ in t.calls)   # ticker-only feed never called
    assert "finnhub:needs_symbol" in res.note


def test_no_key_providers_are_skipped():
    t = FakeTransport({"gnews.io": {"articles": [
        {"title": "T", "url": "https://g/2", "source": {"name": "G"},
         "publishedAt": "2026-06-05"}]}})
    res = _news(t, news_gnews_io_key="K").search("steel")
    assert res.status == "ok" and res.note.startswith("served_by=gnews")
    assert "marketaux:no_key" in res.note          # earlier providers skipped for no key


def test_all_unavailable_returns_failed():
    t = FakeTransport({}, fail_for=("marketaux.com",))
    res = _news(t, news_marketaux_key="K").search("x")
    assert res.status == "failed" and res.items == []
    assert "tried=['marketaux']" in res.note


# --- finnhub ticker window + parse ----------------------------------------
def test_finnhub_company_news_window_and_parse():
    t = FakeTransport({"finnhub.io": [
        {"headline": "AAPL launches", "summary": "s", "url": "https://f/1",
         "source": "Finnhub", "datetime": 1749081600, "related": "AAPL"}]})
    res = _news(t, news_finnhub_key="K").search("apple", symbol="AAPL",
                                                anchor_date="2026-06-06")
    assert res.status == "ok" and res.note.startswith("served_by=finnhub")
    params = next(p for u, p in t.calls if "finnhub.io" in u)
    assert params["symbol"] == "AAPL" and params["to"] == "2026-06-06"
    assert params["from"] == "2026-05-07"           # 30-day window from anchor
    art = res.items[0]
    assert art.claim == "AAPL launches"
    assert len(art.citations[0].locator["published_at"]) == 10   # epoch -> ISO date


# --- typed model out --------------------------------------------------------
def test_search_articles_returns_typed_model():
    t = FakeTransport({"marketaux.com": _MARKETAUX})
    arts = _news(t, news_marketaux_key="K").search_articles("Millat", symbol="MTL")
    assert len(arts) == 1 and isinstance(arts[0], NewsArticle)
    a = arts[0]
    assert a.provider == "marketaux" and a.source == "Business Recorder"
    assert a.url == "https://x/mtl" and a.content and a.symbols == ["MTL"]


# --- direct parser field-mapping locks (content + source) ------------------
def test_parsers_map_fields_per_provider():
    a = NP._parse_alphavantage({"feed": [
        {"title": "T", "summary": "S", "url": "u", "source": "AV",
         "time_published": "20260605T120000", "overall_sentiment_score": 0.1,
         "ticker_sentiment": [{"ticker": "MTL"}]}]})[0]
    assert a.provider == "alphavantage" and a.published_at == "2026-06-05" and a.symbols == ["MTL"]

    g = NP._parse_gnews({"articles": [
        {"title": "T", "description": "d", "content": "c", "url": "u",
         "source": {"name": "GNews"}, "publishedAt": "2026-06-05"}]})[0]
    assert g.provider == "gnews" and g.source == "GNews" and g.content == "c"

    w = NP._parse_worldnews({"news": [
        {"title": "T", "text": "body", "url": "u", "publish_date": "2026-06-05 10:00:00",
         "source_country": "pk"}]})[0]
    assert w.provider == "worldnews" and w.content == "body"

    e = NP._parse_newsapi_ai({"articles": {"results": [
        {"title": "T", "body": "long body", "url": "u",
         "source": {"title": "Reuters"}, "dateTime": "2026-06-05T00:00:00Z"}]}})[0]
    assert e.provider == "newsapi_ai" and e.source == "Reuters" and e.content == "long body"

    # malformed / error bodies -> []
    assert NP._parse_marketaux("nope") == []
    assert NP._parse_newsapi_org({"status": "error"}) == []
    assert NP._parse_newsdata({"status": "error"}) == []
