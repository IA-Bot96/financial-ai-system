"""News semantic retrieval: chunk -> embed -> rank vs query -> recency -> dedup,
plus the downstream guarantees (distinct-article citations, external premises reach
the LLM, and figures quoted from a cited article pass the numeric guard).

Offline: a deterministic fake embedder over a tiny vocab (no model download)."""

from types import SimpleNamespace

import numpy as np
import pytest

from app.engines.fie import news_retrieval as NR
from app.engines.fie import synthesis, safety, citations as cit
from app.engines.fie.models import Citation, EvidenceItem, QueryFrame

_VOCAB = ["expansion", "profit", "weather", "capacity", "cement"]


class FakeEmbedder:
    """text -> normalized count vector over _VOCAB (+ a tiny null dim so norm>0)."""
    def encode(self, texts, normalize_embeddings=True, **kw):
        rows = []
        for t in texts:
            tl = (t or "").lower()
            v = np.array([float(tl.count(w)) for w in _VOCAB] + [0.01])
            n = np.linalg.norm(v)
            rows.append(v / n if n else v)
        return np.array(rows)


def _settings(**over):
    base = dict(news_min_body_chars=600, news_chunk_chars=500, news_chunk_overlap=100,
                news_similarity_floor=0.2, news_top_k=8, news_dedup_similarity=0.92,
                news_same_story_similarity=0.85,
                news_recency_weight=0.2, news_recency_halflife_days=14,
                embedding_model="fake")
    base.update(over)
    return SimpleNamespace(**base)


def _article(title, snippet, source, url, date, *, author=None, content=None):
    loc = {"source": source, "author": author, "provider": "p", "url": url, "link": url,
           "published_at": date, "snippet": snippet, "content": content, "symbols": []}
    return EvidenceItem(claim=title, kind="external",
                        citations=[Citation(ref_id="C?", kind="external",
                                            display=source, locator=loc)])


# --- chunking --------------------------------------------------------------
def test_sliding_window_long_body_multiple_chunks():
    art = _article("MLCF capacity", "expansion in capacity", "Reuters", "u1",
                   "2026-06-05", content="cement expansion " * 120)  # ~2000 chars
    chunks = NR._article_chunks(art, _settings())
    assert len(chunks) > 1                       # long body -> windowed
    # short item -> single chunk (the snippet)
    short = _article("t", "brief expansion note", "Reuters", "u2", "2026-06-05")
    assert len(NR._article_chunks(short, _settings())) == 1


def test_build_query_text_uses_company_and_metrics():
    f = QueryFrame(raw_query="expansion plans", intent="news_impact",
                   company="MLCF", metrics=["sales"])
    q = NR.build_query_text(f, "MLCF")
    assert "MLCF" in q and "expansion" in q and "sales" in q


# --- rank + recency + dedup ------------------------------------------------
def test_retrieve_ranks_dedupes_and_drops_irrelevant():
    arts = [
        # A & B: near-identical syndicated story (different outlets); B is newer
        _article("MLCF announces expansion", "significant expansion in capacity",
                 "Reuters", "u_a", "2026-05-01"),
        _article("MLCF announces expansion plan", "significant expansion in capacity",
                 "WSJ", "u_b", "2026-06-05"),
        # D: distinct, also relevant to the query's 'profit'
        _article("MLCF profit rises", "quarterly profit rises sharply",
                 "Bloomberg", "u_d", "2026-06-04"),
        # C: irrelevant
        _article("Weekend outlook", "weather forecast for the weekend",
                 "MetService", "u_c", "2026-06-05"),
    ]
    out = NR.retrieve(arts, "MLCF expansion profit", settings=_settings(),
                      embedder=FakeEmbedder(), anchor_date="2026-06-06")
    srcs = [e.citations[0].locator["source"] for e in out]
    assert "MetService" not in srcs               # below similarity floor -> dropped
    assert "Reuters" not in srcs                  # syndication dup of WSJ -> dropped (older)
    assert set(srcs) == {"WSJ", "Bloomberg"}      # one of the dup pair + the distinct story
    # each surviving chunk carries provenance + a relevance score
    for e in out:
        loc = e.citations[0].locator
        assert loc["link"] and loc["source"] and "relevance" in loc


def test_news_independence_folds_syndication_and_counts_origins():
    # one story carried by 3 outlets (near-identical) + one genuinely distinct story
    arts = [
        _article("MLCF announces expansion", "significant expansion in capacity",
                 "Reuters", "u_r", "2026-06-05"),
        _article("MLCF expansion update", "significant expansion in capacity",
                 "WSJ", "u_w", "2026-06-05"),
        _article("MLCF expansion confirmed", "significant expansion in capacity",
                 "Dawn", "u_d", "2026-06-05"),
        _article("MLCF quarterly profit rises", "quarterly profit rises sharply",
                 "Bloomberg", "u_b", "2026-06-04"),
    ]
    out = NR.retrieve(arts, "MLCF expansion profit", settings=_settings(),
                      embedder=FakeEmbedder(), anchor_date="2026-06-06")
    # 3 reprints collapse to ONE independent story; profit is a second -> 2 reps
    assert len(out) == 2
    by = {e.citations[0].locator["source"]: e for e in out}
    rep = next(e for e in out if "expansion" in e.claim.lower())
    syn = rep.citations[0].locator["syndicated_in"]
    assert set(syn) >= {"Reuters", "WSJ", "Dawn"}          # all 3 outlets folded into one origin
    # independence metric: 2 distinct stories, not 4 "confirmations"
    assert rep.citations[0].locator["independent_stories"] == 2
    assert out[0].citations[0].locator["corroboration_strength"] == 0.5  # 1 - 0.5^(2-1)


def test_retrieve_fallback_without_model(monkeypatch):
    import app.engines.extraction.services.embeddings as emb
    monkeypatch.setattr(emb, "get_embedder", lambda name: None)   # no model available
    arts = [_article("old", "expansion", "A", "u1", "2026-01-01"),
            _article("new", "expansion", "B", "u2", "2026-06-05")]
    out = NR.retrieve(arts, "expansion", settings=_settings(), anchor_date="2026-06-06")
    assert [e.citations[0].locator["source"] for e in out] == ["B", "A"]   # recency order


def test_retrieve_empty_input():
    assert NR.retrieve([], "q", settings=_settings(), embedder=FakeEmbedder()) == []


# --- citation binding: distinct articles must NOT collapse -----------------
def test_distinct_articles_get_distinct_citations():
    a = _article("t1", "s1", "Reuters", "https://r/1", "2026-06-05")
    b = _article("t2", "s2", "WSJ", "https://w/2", "2026-06-05")
    cites, _ = cit.bind([a, b], [])
    links = {c.locator.get("link") for c in cites}
    assert len(cites) == 2 and links == {"https://r/1", "https://w/2"}


def test_same_article_chunks_merge_to_one_citation():
    base = _article("t", "s", "WSJ", "https://w/1", "2026-06-05")
    c1 = base.model_copy(deep=True); c1.citations[0].locator["chunk_id"] = 0
    c2 = base.model_copy(deep=True); c2.citations[0].locator["chunk_id"] = 1
    cites, _ = cit.bind([c1, c2], [])
    assert len(cites) == 1                          # same link -> one source citation


# --- synthesis: external news reaches the LLM premises, with source + ref ---
def test_external_news_becomes_a_premise():
    e = _article("MLCF expands", "plans Punjab plant", "Reuters", "u1", "2026-06-05")
    e.citations[0].ref_id = "C1"
    e.citations[0].locator["chunk_text"] = "plans a new Punjab plant"
    f = QueryFrame(raw_query="news", intent="news_impact", company="MLCF")
    g = synthesis.build_graph(f, [e], [], [])
    prem = " ".join(g.premises)
    assert "MLCF expands" in prem and "Reuters" in prem and "[C1]" in prem


# --- safety: a figure quoted from a cited article is backed ----------------
def test_number_from_cited_article_passes_guard():
    e = _article("MLCF profit", "profit rose 25% YoY", "WSJ", "u1", "2026-06-05")
    e.citations[0].ref_id = "C1"
    e.citations[0].locator["chunk_text"] = "profit rose 25% YoY"
    f = QueryFrame(raw_query="news", intent="news_impact", company="MLCF")
    cites = [e.citations[0]]
    assert safety.verify_prose("Profit rose 25% per WSJ [C1].", f, [e], [], cites)
    # a number NOT present in any cited source is still rejected
    assert not safety.verify_prose("Profit rose 88% [C1].", f, [e], [], cites)
