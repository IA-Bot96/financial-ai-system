"""PSX symbols-master adapter (L3b).

GET https://dps.psx.com.pk/symbols -> the registry of listed securities
({symbol, name, sectorName, isETF, isDebt, isGEM}). Used to resolve a company
name to its ticker (replacing the hardcoded map) and to look up sectors / peers.
Fetched once and cached (failure_mode=cache).
"""

from __future__ import annotations

from rapidfuzz import fuzz, process

from .base import ApiClient, ApiSpec
from .parsers import parse_symbols_master
from ..models import Citation, EvidenceItem


def _normalizer(raw, params, spec, retrieved_at):
    items = []
    for rec in parse_symbols_master(raw):
        cite = Citation(ref_id="C?", kind="external",
                        display=f"PSX symbols: {rec['symbol']}",
                        locator={"source": spec.id, "retrieved_at": retrieved_at, **rec},
                        retrieved_at=retrieved_at)
        items.append(EvidenceItem(claim=rec.get("name") or rec["symbol"], kind="external",
                                  citations=[cite], reliability=spec.reliability_rating))
    return items


class Symbols:
    def __init__(self, client: ApiClient, base_url: str = "https://dps.psx.com.pk") -> None:
        self.client = client
        self.spec = ApiSpec(id="PSX.Symbols", base_url=base_url, path="symbols",
                            method="GET", response_type="json", reliability_rating=0.95,
                            refresh_frequency="daily", failure_mode="cache",
                            normalizer=_normalizer)
        self._records: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._records is None:
            res = self.client.call(self.spec)
            self._records = [c.locator for i in res.items for c in i.citations]
        return self._records

    def ticker_for(self, name: str, *, threshold: float = 88.0) -> str | None:
        """Fuzzy company-name -> ticker. token_set_ratio so a partial name
        ("millat tractors") still matches the full registry name."""
        recs = self._load()
        if not name or not recs:
            return None
        names = [r.get("name") or "" for r in recs]
        m = process.extractOne(name, names, scorer=fuzz.token_set_ratio,
                               processor=str.lower, score_cutoff=threshold)
        return recs[m[2]]["symbol"] if m else None

    def sector_for(self, symbol: str) -> str | None:
        for r in self._load():
            if r.get("symbol") == symbol:
                return r.get("sector")
        return None

    def by_sector(self, sector: str) -> list[str]:
        s = (sector or "").lower()
        return [r["symbol"] for r in self._load() if (r.get("sector") or "").lower() == s]

    def records(self) -> list[dict]:
        """All normalized registry records ({symbol, name, sector, is_etf, is_debt, is_gem})."""
        return list(self._load())

    def name_for(self, symbol: str) -> str | None:
        for r in self._load():
            if (r.get("symbol") or "").upper() == (symbol or "").upper():
                return r.get("name")
        return None
