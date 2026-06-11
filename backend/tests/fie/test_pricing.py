"""Token -> USD cost estimation (pricing.py). Pure functions; no workbook fixtures."""

import pytest

from app.engines.fie import pricing


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """The rate table is cached at module level; reset around each test so a FIE_MODEL_PRICING
    override (or its absence) is picked up cleanly and doesn't leak between tests."""
    pricing._cache = None
    pricing._warned_models.clear()
    yield
    pricing._cache = None
    pricing._warned_models.clear()


def test_estimate_cost_at_one_million_tokens_equals_rate():
    c = pricing.estimate_cost("gpt-5.4-mini", 1_000_000, 1_000_000)
    assert c["input_usd"] == 0.25 and c["output_usd"] == 2.00
    assert c["total_usd"] == 2.25
    assert c["input_rate_per_1m"] == 0.25 and c["output_rate_per_1m"] == 2.00


def test_estimate_cost_scales_and_rounds():
    c = pricing.estimate_cost("gpt-4o-mini", 8_000, 200)  # 0.15/1M in, 0.60/1M out
    assert c["input_usd"] == round(8_000 / 1e6 * 0.15, 6)
    assert c["output_usd"] == round(200 / 1e6 * 0.60, 6)
    assert c["total_usd"] == round(c["input_usd"] + c["output_usd"], 6)


def test_estimate_cost_zero_tokens_is_zero():
    c = pricing.estimate_cost("gpt-4o", 0, 0)
    assert c["total_usd"] == 0.0


def test_estimate_cost_negative_tokens_clamped():
    assert pricing.estimate_cost("gpt-4o", -5, -5)["total_usd"] == 0.0


def test_rates_exact_match():
    assert pricing._rates_for("gpt-4o") == {"input": 2.50, "output": 10.00}


def test_rates_longest_prefix_match():
    # a dated/full id resolves to its family by longest prefix (gpt-4o, not the fallback)
    assert pricing._rates_for("gpt-4o-2024-08-06") == {"input": 2.50, "output": 10.00}
    # a mini variant prefers the more specific 'gpt-4o-mini' over 'gpt-4o'
    assert pricing._rates_for("gpt-4o-mini-2024") == {"input": 0.15, "output": 0.60}


def test_rates_unknown_model_falls_back():
    assert pricing._rates_for("some-other-vendor-model") == pricing._FALLBACK


def test_unknown_model_warns_once(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="app.engines.fie"):
        pricing._rates_for("mystery-model")
        pricing._rates_for("mystery-model")  # second call must NOT log again
    warns = [r for r in caplog.records if "no pricing for model" in r.getMessage()
             and "mystery-model" in r.getMessage()]
    assert len(warns) == 1


def test_env_override_replaces_rate(monkeypatch):
    monkeypatch.setenv("FIE_MODEL_PRICING", '{"gpt-5.4-mini": {"input": 1.0, "output": 3.0}}')
    pricing._cache = None  # force re-read with the override in place
    c = pricing.estimate_cost("gpt-5.4-mini", 1_000_000, 1_000_000)
    assert c["input_usd"] == 1.0 and c["output_usd"] == 3.0


def test_env_override_invalid_json_is_ignored(monkeypatch):
    monkeypatch.setenv("FIE_MODEL_PRICING", "this is not json")
    pricing._cache = None
    # falls back to the built-in defaults instead of crashing
    c = pricing.estimate_cost("gpt-5.4-mini", 1_000_000, 0)
    assert c["input_usd"] == 0.25
