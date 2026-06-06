"""Entity registry with resolved / review / quarantine verdicts (L1 support).

Ported in spirit from the legacy MSIL ``entity_resolver`` / ``entity_registry``: a
typed-alias registry over the PSX symbols master that resolves a company string to a
ticker through an explicit ladder and returns a *verdict* — RESOLVED / REVIEW /
QUARANTINED — rather than a bare best-guess. This replaces the silent rapidfuzz bind
(``Symbols.ticker_for``) for the cases that matter: a low-confidence fuzzy hit or a
typo/unknown ticker-shaped token no longer binds to a wrong symbol; it is quarantined.

We keep the *mechanism* from legacy (typed aliases, the ladder, anti-contamination)
and repopulate it from our own PSX symbols list — the MVP Lucky/Millat seed is NOT
carried over. Anti-contamination here is generic:

  * ticker-shaped unknown guard — a bare all-caps token that is not an exact ticker
    is quarantined instead of fuzzy-bound to a near ticker (catches "LUK"/"LUKX").
  * close-rivals guard — when the top two fuzzy candidates are near-tied the result
    is REVIEW (ambiguous bare group token), with both surfaced as candidates.
  * an explicit, caller-extensible ``quarantine_terms`` set for known confusions.

Ladder (confidence on a 0–1 scale):
  exact ticker     0.99  RESOLVED
  exact legal name 0.98  RESOLVED
  alias variant    0.95  RESOLVED
  fuzzy >= 0.88          RESOLVED
  fuzzy >= 0.70          REVIEW       (requires_confirmation)
  fuzzy >= 0.62          QUARANTINED
  otherwise             —            QUARANTINED (no ticker)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

from rapidfuzz import fuzz, process

# floors (0–1). RESOLVED floor matches the legacy Symbols.ticker_for cutoff (88).
RESOLVED_FLOOR = 0.88
REVIEW_FLOOR = 0.70
QUARANTINE_FLOOR = 0.62
# top-two within this margin on a fuzzy hit => ambiguous => REVIEW (close-rivals)
CLOSE_RIVAL_MARGIN = 0.05

_TICKER_SHAPED = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")  # 2–6 char all-caps alnum token


class AliasType(str, Enum):
    TICKER = "ticker"
    LEGAL_NAME = "legal_name"
    NAME_VARIANT = "name_variant"
    ISIN = "isin"
    SECP_REG_NO = "secp_reg_no"


class ReviewStatus(str, Enum):
    RESOLVED = "resolved"
    REVIEW = "review"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class EntityAlias:
    value: str
    alias_type: AliasType
    exact_match: bool = True
    historical: bool = False
    requires_confirmation: bool = False


@dataclass
class Entity:
    ticker: str
    legal_name: str
    sector: Optional[str] = None
    aliases: list[EntityAlias] = field(default_factory=list)


@dataclass
class Resolution:
    """Verdict for one company->ticker resolution attempt."""
    query: str
    ticker: Optional[str]
    status: ReviewStatus
    confidence: float
    method: str
    resolution_path: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.status is ReviewStatus.RESOLVED


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


class EntityRegistry:
    """Typed-alias registry over PSX symbols with verdict-returning resolution."""

    def __init__(self, entities: Iterable[Entity], *,
                 quarantine_terms: Iterable[str] = ()) -> None:
        self.entities: list[Entity] = list(entities)
        self._quarantine = {_norm(t) for t in quarantine_terms}
        # lookup indexes
        self._by_ticker: dict[str, Entity] = {}
        self._by_name: dict[str, Entity] = {}      # legal name + variant aliases
        self._fuzzy_keys: list[tuple[str, Entity]] = []  # (key, entity) for fuzzy
        for e in self.entities:
            self._by_ticker[_norm(e.ticker)] = e
            self._by_name[_norm(e.legal_name)] = e
            self._fuzzy_keys.append((e.legal_name, e))
            for a in e.aliases:
                if a.alias_type in (AliasType.NAME_VARIANT, AliasType.LEGAL_NAME):
                    self._by_name.setdefault(_norm(a.value), e)
                    self._fuzzy_keys.append((a.value, e))

    # --- construction ------------------------------------------------------
    @classmethod
    def from_records(cls, records: Iterable[dict], *,
                     quarantine_terms: Iterable[str] = ()) -> "EntityRegistry":
        """Build from PSX symbols-master records ({symbol, name, sector, ...})."""
        ents = []
        for r in records:
            sym = r.get("symbol")
            if not sym:
                continue
            ents.append(Entity(ticker=sym, legal_name=r.get("name") or sym,
                               sector=r.get("sector")))
        return cls(ents, quarantine_terms=quarantine_terms)

    @classmethod
    def from_symbols(cls, symbols_adapter, *,
                     quarantine_terms: Iterable[str] = ()) -> "EntityRegistry":
        """Build from a live ``Symbols`` adapter (uses its cached records)."""
        return cls.from_records(symbols_adapter._load(),
                                quarantine_terms=quarantine_terms)

    # --- resolution --------------------------------------------------------
    def resolve(self, query: str) -> Resolution:
        q = (query or "").strip()
        path: list[str] = []
        if not q or not self.entities:
            return Resolution(query, None, ReviewStatus.QUARANTINED, 0.0,
                              "empty", ["empty"], [])

        qn = _norm(q)

        # 0. explicit quarantine terms (known confusions) — never auto-bind
        if qn in self._quarantine:
            return Resolution(query, None, ReviewStatus.QUARANTINED, 0.0,
                              "quarantine_term", ["quarantine_term"], [])

        # 1. exact ticker
        path.append("exact_ticker")
        e = self._by_ticker.get(qn)
        if e:
            return Resolution(query, e.ticker, ReviewStatus.RESOLVED, 0.99,
                              "exact_ticker", path, [e.ticker])

        # ticker-shaped unknown guard: a bare all-caps token that is NOT a known
        # ticker must not fuzzy-bind to a near ticker — quarantine it.
        if _TICKER_SHAPED.match(q):
            path.append("ticker_shaped_unknown")
            return Resolution(query, None, ReviewStatus.QUARANTINED, 0.0,
                              "ticker_shaped_unknown", path, [])

        # 2. exact legal name / variant
        path.append("exact_name")
        e = self._by_name.get(qn)
        if e:
            method = "exact_legal_name" if qn == _norm(e.legal_name) else "alias"
            conf = 0.98 if method == "exact_legal_name" else 0.95
            return Resolution(query, e.ticker, ReviewStatus.RESOLVED, conf,
                              method, path, [e.ticker])

        # 3. fuzzy over names + variant aliases
        path.append("fuzzy")
        names = [k for k, _ in self._fuzzy_keys]
        hits = process.extract(q, names, scorer=fuzz.token_set_ratio,
                               processor=str.lower, limit=5)
        if not hits:
            return Resolution(query, None, ReviewStatus.QUARANTINED, 0.0,
                              "no_match", path, [])
        top_key, top_score, top_idx = hits[0]
        score = top_score / 100.0
        top_entity = self._fuzzy_keys[top_idx][1]

        # close-rivals guard: a near-tie against a *different* entity is ambiguous
        rival = next(((k, s, i) for (k, s, i) in hits[1:]
                      if self._fuzzy_keys[i][1].ticker != top_entity.ticker), None)
        candidates = [top_entity.ticker]
        if rival and (score - rival[1] / 100.0) <= CLOSE_RIVAL_MARGIN and score >= QUARANTINE_FLOOR:
            rival_entity = self._fuzzy_keys[rival[2]][1]
            candidates.append(rival_entity.ticker)
            path.append("close_rivals")
            return Resolution(query, top_entity.ticker, ReviewStatus.REVIEW,
                              score, "fuzzy_ambiguous", path, candidates)

        if score >= RESOLVED_FLOOR:
            status, method = ReviewStatus.RESOLVED, "fuzzy"
        elif score >= REVIEW_FLOOR:
            status, method = ReviewStatus.REVIEW, "fuzzy_low"
        elif score >= QUARANTINE_FLOOR:
            status, method = ReviewStatus.QUARANTINED, "fuzzy_quarantine"
        else:
            return Resolution(query, None, ReviewStatus.QUARANTINED, score,
                              "below_floor", path, [])

        ticker = top_entity.ticker if status is not ReviewStatus.QUARANTINED else None
        return Resolution(query, ticker, status, score, method, path, candidates)
