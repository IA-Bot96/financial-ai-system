"""Glossary §2 modifier guard: a concept-changing modifier (adjusted/normalized/
underlying/forward/ntm) must not be collapsed onto the base canonical."""
from app.engines.extraction.services.metric_resolver import get_resolver


def _key(label):
    m = get_resolver().resolve(label)
    return m.canonical_key if m else None


def test_modifier_mismatch_mechanism():
    r = get_resolver()
    assert r._modifier_mismatch("adjusted ebitda", "ebitda") is True
    assert r._modifier_mismatch("normalized ebitda", "ebitda") is True
    assert r._modifier_mismatch("underlying ebitda", "ebitda") is True
    assert r._modifier_mismatch("forward p/e", "price_to_earnings") is True
    assert r._modifier_mismatch("ebitda", "ebitda") is False          # base concept, no modifier
    assert r._modifier_mismatch("trailing p/e", "price_to_earnings") is False   # trailing == base
    assert r._modifier_mismatch("ttm p/e", "price_to_earnings") is False
    assert r._modifier_mismatch("net sales", "revenue") is False       # "net" is not a guarded modifier


def test_modified_captions_do_not_collapse_onto_base():
    assert _key("Adjusted EBITDA") != "ebitda"
    assert _key("Normalized EBITDA") != "ebitda"
    assert _key("Underlying EBITDA") != "ebitda"
    assert _key("Forward P/E") != "price_to_earnings"


def test_base_and_unguarded_captions_still_resolve():
    assert _key("EBITDA") == "ebitda"
    assert _key("Net Sales") == "revenue"           # "net" not guarded
    assert _key("Current Assets") == "current_assets"   # "current" not guarded (own canonical)
    assert _key("Diluted EPS") == "diluted_earnings_per_share"   # own canonical, not blocked
