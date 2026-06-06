"""Guards for glossary §4 ambiguous terms: a bare context-dependent caption must NOT
auto-map to a sector-specific (banking) canonical, while precise captions still resolve."""
from app.engines.extraction.services.metric_resolver import get_resolver


def _key(label):
    m = get_resolver().resolve(label)
    return m.canonical_key if m else None


def test_bare_ambiguous_terms_do_not_map_to_banking_metrics():
    # "deposits" (could be bank-deposit asset / security deposit) must not become the
    # banking liability customer_deposits; "advances" must not become the loan-book
    # metric gross_advances. They should go to review instead.
    assert _key("deposits") != "customer_deposits"
    assert _key("advances") != "gross_advances"


def test_precise_captions_still_resolve():
    assert _key("customer deposits") == "customer_deposits"     # explicit -> bank liability ok
    assert _key("long term deposits") == "long_term_deposits"
    assert _key("advances to suppliers") == "loans_and_advances"
    assert _key("advances to employees") == "loans_to_employees"
    assert _key("short-term loans and advances") == "loans_and_advances"
