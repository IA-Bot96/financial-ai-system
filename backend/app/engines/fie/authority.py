"""Claim-type-scoped authority matrix (L6).

Ported from the legacy MSIL authority matrix: trust is **contextual** — it depends on
the *kind of claim*, not a single per-source rating. An audited workbook dominates
financial facts, but an analyst/explicit-guidance forecast can outrank the annual
report's own optimistic outlook; market data is authoritative for an observed price
but can never *create* a financial-statement fact.

`effective_rank(claim_type, authority_class)` is the primitive our conflict resolver
and evidence ranker lacked. Two special rules encode the trust model declaratively:
news media and market-revealed data may corroborate / surface contradictions but
**cannot create a standalone fact**.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class ClaimType(str, Enum):
    REGULATORY_COMPLIANCE = "regulatory_compliance"
    CORPORATE_ACTION_FACT = "corporate_action_fact"
    AUDITED_FACT = "audited_fact"
    OFFICIAL_UNAUDITED_FACT = "official_unaudited_fact"
    FORWARD_EXPECTATION = "forward_expectation"
    DESCRIPTIVE = "descriptive"
    SENTIMENT = "sentiment"
    SECTOR_CONTEXT = "sector_context"


class AuthorityClass(str, Enum):
    REGULATORY_INDEPENDENT = "regulatory_independent"
    EXCHANGE_OFFICIAL = "exchange_official"
    AUDITED_ISSUER = "audited_issuer"
    OFFICIAL_ISSUER_UNAUDITED = "official_issuer_unaudited"
    INDEPENDENT_OPINION = "independent_opinion"
    SECTOR_AGGREGATE = "sector_aggregate"
    MARKET_REVEALED = "market_revealed"
    NEWS_MEDIA = "news_media"


C, A = ClaimType, AuthorityClass

# claim type -> authority classes, highest authority first (ported verbatim)
AUTHORITY_MATRIX: dict[ClaimType, tuple[AuthorityClass, ...]] = {
    C.REGULATORY_COMPLIANCE: (A.REGULATORY_INDEPENDENT, A.AUDITED_ISSUER,
                              A.OFFICIAL_ISSUER_UNAUDITED, A.NEWS_MEDIA),
    C.CORPORATE_ACTION_FACT: (A.EXCHANGE_OFFICIAL, A.OFFICIAL_ISSUER_UNAUDITED,
                              A.REGULATORY_INDEPENDENT, A.NEWS_MEDIA),
    C.AUDITED_FACT: (A.AUDITED_ISSUER, A.OFFICIAL_ISSUER_UNAUDITED,
                     A.INDEPENDENT_OPINION, A.NEWS_MEDIA),
    C.OFFICIAL_UNAUDITED_FACT: (A.OFFICIAL_ISSUER_UNAUDITED, A.EXCHANGE_OFFICIAL,
                                A.INDEPENDENT_OPINION, A.NEWS_MEDIA),
    C.FORWARD_EXPECTATION: (A.INDEPENDENT_OPINION, A.OFFICIAL_ISSUER_UNAUDITED,
                            A.MARKET_REVEALED, A.AUDITED_ISSUER, A.NEWS_MEDIA),
    C.DESCRIPTIVE: (A.OFFICIAL_ISSUER_UNAUDITED, A.AUDITED_ISSUER, A.SECTOR_AGGREGATE,
                    A.INDEPENDENT_OPINION, A.NEWS_MEDIA),
    C.SENTIMENT: (A.MARKET_REVEALED, A.INDEPENDENT_OPINION, A.NEWS_MEDIA, A.SECTOR_AGGREGATE),
    C.SECTOR_CONTEXT: (A.SECTOR_AGGREGATE, A.INDEPENDENT_OPINION, A.MARKET_REVEALED, A.NEWS_MEDIA),
}

# authority classes that may corroborate but NOT create a standalone fact
_OBSERVATION_ONLY = {A.NEWS_MEDIA, A.MARKET_REVEALED}


def validate_matrix() -> None:
    """Totality invariant: every claim type has a non-empty, duplicate-free ranking.
    Fails fast (call at import/boot) so the trust config can't silently develop a hole."""
    for ct in ClaimType:
        order = AUTHORITY_MATRIX.get(ct)
        if not order:
            raise ValueError(f"authority matrix missing claim type: {ct.value}")
        if len(set(order)) != len(order):
            raise ValueError(f"authority matrix has duplicate classes for {ct.value}")


validate_matrix()


def effective_rank(claim_type: ClaimType, authority_class: AuthorityClass) -> Optional[int]:
    """0 = most authoritative; None if the class isn't ranked for this claim type."""
    order = AUTHORITY_MATRIX[claim_type]
    return order.index(authority_class) if authority_class in order else None


def authority_weight(claim_type: ClaimType, authority_class: AuthorityClass) -> float:
    """Normalized 0..1 authority (1.0 = top of the ranking for this claim type),
    0.0 if unranked — for use as a ranking/scoring signal."""
    order = AUTHORITY_MATRIX[claim_type]
    r = effective_rank(claim_type, authority_class)
    if r is None:
        return 0.0
    return 1.0 if len(order) == 1 else round(1.0 - r / (len(order) - 1), 6)


def can_create_standalone_fact(authority_class: AuthorityClass) -> bool:
    """News/market may corroborate or contradict, but never originate a fact."""
    return authority_class not in _OBSERVATION_ONLY


# --- mapping our evidence -> (claim_type, authority_class) ------------------
# admission role (see admission.py) + source id -> authority class
def authority_class_for(ev) -> AuthorityClass:
    role = getattr(ev, "role", None)
    loc = ev.citations[0].locator if getattr(ev, "citations", None) else {}
    src = (loc.get("source") or "").lower()
    if role == "baseline":
        return A.AUDITED_ISSUER
    if role == "event_fact":
        return A.EXCHANGE_OFFICIAL
    if role == "non_authoritative":
        return A.NEWS_MEDIA
    if role == "forecast_context":
        return A.INDEPENDENT_OPINION
    # supporting: discriminate by source
    if "secp" in src:
        return A.REGULATORY_INDEPENDENT
    if "sector" in src or "macro" in src:
        return A.SECTOR_AGGREGATE
    if "announcement" in src:
        return A.OFFICIAL_ISSUER_UNAUDITED
    return A.EXCHANGE_OFFICIAL         # overview / quote / market watch / screener (exchange-published)


def claim_type_for(ev) -> ClaimType:
    role = getattr(ev, "role", None)
    loc = ev.citations[0].locator if getattr(ev, "citations", None) else {}
    src = (loc.get("source") or "").lower()
    if role == "baseline":
        return C.AUDITED_FACT
    if role == "event_fact":
        return C.CORPORATE_ACTION_FACT
    if role == "non_authoritative":
        return C.SENTIMENT
    if role == "forecast_context":
        return C.FORWARD_EXPECTATION
    if "secp" in src:
        return C.REGULATORY_COMPLIANCE
    if "sector" in src:
        return C.SECTOR_CONTEXT
    if "overview" in src or "quote" in src or "screener" in src or "market" in src:
        return C.OFFICIAL_UNAUDITED_FACT      # exchange-observed current value
    return C.DESCRIPTIVE


def resolve(a, b, *, claim_type: Optional[ClaimType] = None) -> dict:
    """Decide which of two evidence items wins for the same topic, by authority rank
    (lower rank wins), with recency as the tie-breaker. Returns
    {winner: 'a'|'b'|'tie', rationale, a_rank, b_rank}."""
    ct = claim_type or claim_type_for(a)
    ra = effective_rank(ct, authority_class_for(a))
    rb = effective_rank(ct, authority_class_for(b))
    ax = ra if ra is not None else 99
    bx = rb if rb is not None else 99
    if ax != bx:
        win = "a" if ax < bx else "b"
        return {"winner": win, "rationale": f"higher authority for {ct.value}",
                "a_rank": ra, "b_rank": rb}
    fa, fb = getattr(a, "freshness", None) or "", getattr(b, "freshness", None) or ""
    if fa != fb:
        return {"winner": "a" if fa > fb else "b",
                "rationale": "equal authority — newer evidence", "a_rank": ra, "b_rank": rb}
    return {"winner": "tie", "rationale": "equal authority and recency",
            "a_rank": ra, "b_rank": rb}
