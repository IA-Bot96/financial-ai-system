"""Entity registry verdicts: a company string resolves through an explicit ladder
to RESOLVED / REVIEW / QUARANTINED, and a typo/unknown ticker-shaped token never
silently binds to a wrong symbol."""

from app.engines.fie.entity_registry import EntityRegistry, ReviewStatus

# a small slice of a PSX-symbols-master shape ({symbol, name, sector})
_RECORDS = [
    {"symbol": "MTL", "name": "Millat Tractors Limited", "sector": "Auto Assembler"},
    {"symbol": "LUCK", "name": "Lucky Cement Limited", "sector": "Cement"},
    {"symbol": "MLCF", "name": "Maple Leaf Cement Factory Limited", "sector": "Cement"},
    {"symbol": "ENGRO", "name": "Engro Corporation Limited", "sector": "Fertilizer"},
    {"symbol": "EFERT", "name": "Engro Fertilizers Limited", "sector": "Fertilizer"},
]


def _reg(**kw):
    return EntityRegistry.from_records(_RECORDS, **kw)


# --- exact tiers -----------------------------------------------------------
def test_exact_ticker_resolves():
    r = _reg().resolve("MTL")
    assert r.status is ReviewStatus.RESOLVED and r.ticker == "MTL"
    assert r.method == "exact_ticker" and r.confidence == 0.99


def test_exact_legal_name_resolves_case_insensitive():
    r = _reg().resolve("lucky cement limited")
    assert r.status is ReviewStatus.RESOLVED and r.ticker == "LUCK"
    assert r.method == "exact_legal_name"


def test_partial_name_fuzzy_resolves():
    r = _reg().resolve("Millat Tractors")
    assert r.status is ReviewStatus.RESOLVED and r.ticker == "MTL"


# --- anti-contamination: ticker-shaped unknown -----------------------------
def test_unknown_ticker_shaped_token_is_quarantined():
    # "LUK" is a typo of LUCK — must NOT fuzzy-bind, must quarantine.
    r = _reg().resolve("LUK")
    assert r.status is ReviewStatus.QUARANTINED and r.ticker is None
    assert r.method == "ticker_shaped_unknown"


def test_unknown_ticker_shaped_token_lukx_is_quarantined():
    r = _reg().resolve("LUCKX")
    assert r.status is ReviewStatus.QUARANTINED and r.ticker is None


# --- anti-contamination: close rivals (ambiguous bare group token) ---------
def test_close_rivals_go_to_review_with_candidates():
    # "Engro Limited" matches both Engro Corporation & Engro Fertilizers equally
    # -> ambiguous bare group token -> REVIEW, both surfaced as candidates.
    r = _reg().resolve("Engro Limited")
    assert r.status is ReviewStatus.REVIEW
    assert set(r.candidates) == {"ENGRO", "EFERT"}


# --- explicit quarantine term ----------------------------------------------
def test_explicit_quarantine_term_blocks_bind():
    r = _reg(quarantine_terms=["massey ferguson"]).resolve("Massey Ferguson")
    assert r.status is ReviewStatus.QUARANTINED and r.ticker is None
    assert r.method == "quarantine_term"


# --- below-floor garbage ----------------------------------------------------
def test_unrelated_string_is_quarantined():
    r = _reg().resolve("some unrelated holding co")
    assert r.status is ReviewStatus.QUARANTINED and r.ticker is None


def test_empty_query_is_quarantined():
    r = _reg().resolve("")
    assert r.status is ReviewStatus.QUARANTINED and r.ticker is None
