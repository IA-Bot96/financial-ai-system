"""Phase 3 — LLM layers behind typed boundaries (with stub clients; no network).

Covers: numeric guard (3.5 hallucination test), LLM understanding fallback (3.1),
LLM narration accepted only when guarded (3.2/3.3), and audience modes (3.4).
"""

import pytest

from app.engines.fie import FinancialIntelligenceEngine
from app.engines.fie import safety
from app.engines.fie.models import QueryFrame


# --- stub LLM clients (deterministic, offline) ---

class HonestNarrator:
    """Writes prose using only figures it is given (echoes the conclusion)."""
    def __init__(self, text):
        self._text = text
    def complete_json(self, system, user, schema):
        return None
    def complete_text(self, system, user):
        return self._text


class HallucinatingNarrator:
    """Injects a fabricated figure not present in the evidence."""
    def complete_json(self, system, user, schema):
        return None
    def complete_text(self, system, user):
        return "The company is clearly worth Rs 999,999,999 and growing at 87.3%."


# --- 3.5 numeric guard ---

def test_guard_accepts_backed_numbers():
    vals, ints = {1.2379, 24459473.0, 19759295.0}, {2024}
    assert safety.numbers_are_backed("It is 1.24x in 2024.", vals, ints)


def test_guard_rejects_unbacked_number():
    vals, ints = {1.2379}, {2024}
    assert not safety.numbers_are_backed("It is 1.24x, worth Rs 999,999,999.", vals, ints)


def test_guard_strips_citation_handles_and_units():
    # [C12] and "Rs '000" must not be read as financial figures
    vals, ints = {91534501.0}, {2024}
    assert safety.numbers_are_backed("Revenue was 91,534,501 (Rs '000) [C12].", vals, ints)


def test_guard_percent_normalization():
    vals, ints = {0.2661}, set()
    assert safety.numbers_are_backed("Gross margin is 26.6%.", vals, ints)
    assert not safety.numbers_are_backed("Gross margin is 40.0%.", vals, ints)


# --- 3.3 hallucinating LLM -> fallback to deterministic, bad number absent ---

def test_hallucinating_llm_is_rejected(millat_store):
    eng = FinancialIntelligenceEngine(millat_store, llm=HallucinatingNarrator())
    r = eng.answer("current ratio for MTL 2024")
    assert r.prose_source == "deterministic"          # LLM prose rejected
    assert "999,999,999" not in r.supporting_analysis  # fabricated figure never shown
    assert "87.3%" not in r.supporting_analysis
    assert r.calculations[0].value is not None         # real answer intact


def test_honest_llm_prose_is_used(millat_store):
    # prose mentions only the backed ratio value
    eng = FinancialIntelligenceEngine(
        millat_store, llm=HonestNarrator("Liquidity is adequate at 1.24x."))
    r = eng.answer("current ratio for MTL 2024")
    assert r.prose_source == "llm"
    assert "1.24x" in r.supporting_analysis


# --- 3.4 audience modes ---

def test_investor_audience_is_concise(millat_store):
    eng = FinancialIntelligenceEngine(millat_store)  # no LLM
    analyst = eng.answer("current ratio for MTL 2024", audience="analyst")
    investor = eng.answer("current ratio for MTL 2024", audience="investor")
    assert analyst.supporting_analysis  # has the formula mechanics
    assert investor.supporting_analysis == ""  # trimmed for investor
    assert analyst.direct_answer == investor.direct_answer  # facts unchanged
