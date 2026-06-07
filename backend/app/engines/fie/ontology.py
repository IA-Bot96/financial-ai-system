"""Metric ontology: map raw workbook labels to canonical metric ids.

Decouples report wording from canonical ids (mirrors the Source Ledger's
``Template label`` vs ``Matched label`` columns). Ships a seed alias set plus a
fuzzy matcher; the alias table grows as data-not-code.

See docs/fie_phase0_foundation.md §6, §7.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

_log = logging.getLogger("app.engines.fie")

# Shared canonical registry produced/used by the extraction engine — the single
# source of truth for label->canonical resolution (419 metrics with aliases).
_DEFAULT_REGISTRY = (
    Path(__file__).resolve().parents[1] / "extraction" / "data" / "canonical_metric_registry.json"
)

# Registry canonical ids that differ from the FIE's stable ids → reconcile to FIE ids
# so formula inputs, fixtures, and STATEMENT_LINE_TO_DETAIL stay unchanged while the
# registry's richer alias coverage still resolves to the right metric.
REGISTRY_TO_FIE = {
    "profit_after_tax": "pat",
    "equity": "total_equity",
    "cash_and_bank_balances": "cash_and_bank",
    "share_capital_and_reserves": "share_capital_reserves",
}

# --- seed canonical metric -> known label variants -------------------------

SEED_ALIASES: dict[str, list[str]] = {
    # P&L
    "revenue": ["revenue from contracts with customers", "net sales", "turnover", "revenue"],
    "cost_of_sales": ["cost of sales", "cost of goods sold", "cost of revenue"],
    "gross_profit": ["gross profit"],
    "distribution_marketing_expenses": ["distribution and marketing expenses", "distribution expenses"],
    "administrative_expenses": ["administrative expenses", "admin expenses"],
    "other_operating_expenses": ["other operating expenses"],
    "total_operating_expenses": ["total operating expenses"],
    "other_income": ["other income"],
    "operating_profit": ["operating profit", "profit from operations"],
    "finance_cost": ["finance cost", "finance costs"],
    "profit_before_tax": ["profit before income tax", "profit before tax", "profit before taxation"],
    "levy": ["levy", "levy final taxes", "levy  final taxes"],
    "taxation": ["taxation income tax", "taxation", "income tax"],
    "pat": [
        "profit after tax",
        "profit after tax for the year",
        "profit for the year",
        "profit after income tax",
    ],
    "oci": ["other comprehensive income"],
    # Balance Sheet
    "non_current_assets": ["non-current assets", "total non-current assets"],
    "current_assets": ["current assets", "total current assets"],
    "total_assets": ["total assets"],
    "share_capital_reserves": ["share capital and reserves", "share capital & reserves"],
    "total_equity": ["total equity", "equity"],
    "non_current_liabilities": ["non-current liabilities", "total non-current liabilities"],
    "current_liabilities": ["current liabilities", "total current liabilities"],
    "total_equity_and_liabilities": ["total equity and liabilities", "total equity & liabilities"],
    "cash_and_bank": ["cash and bank balances", "cash and cash equivalents", "cash & bank balances"],
    "trade_debts": ["trade debts", "trade receivables"],
    "stock_in_trade": ["stock-in-trade", "stock in trade", "inventories"],
    # cash flow
    "operating_cash_flow": ["net cash flows generated from operating activities",
                            "net cash from operating activities", "cash flows from operating activities"],
    "cash_generated_from_operations": ["cash generated from operations"],
    "capex": ["payment made for property plant and equipment",
              "purchase of property plant and equipment", "capital expenditure",
              "additions to property plant and equipment",
              "payments for property plant and equipment"],
    "depreciation_expense": ["depreciation", "depreciation charge", "depreciation for the year"],
    "amortization_expense": ["amortization", "amortisation", "amortization charge"],
    # per-share / valuation inputs
    "shares_outstanding": ["no. of outstanding shares", "number of outstanding shares",
                           "number of ordinary shares", "weighted average number of shares",
                           "issued subscribed and paid up shares"],
    "earnings_per_share": ["earning per share", "earnings per share", "eps", "basic eps"],
    "dividend_per_share": ["dividend per share", "dividend"],
}

# canonical statement-line -> the detail sheet that feeds it (provenance tier 2)
STATEMENT_LINE_TO_DETAIL: dict[str, str] = {
    "revenue": "PL1 - Revenue",
    "cost_of_sales": "PL2 - Cost of Sales",
    "distribution_marketing_expenses": "PL3 - Expenses",
    "administrative_expenses": "PL3 - Expenses",
    "other_operating_expenses": "PL3 - Expenses",
    "total_operating_expenses": "PL3 - Expenses",
    "other_income": "PL4 - Other Income",
    "finance_cost": "PL5 - Finance Cost",
    "levy": "PL6 - Levy",
    "oci": "PL7 - OCI",
    "non_current_assets": "BS1 - Non-Current Assets",
    "current_assets": "BS2 - Current Assets",
    "share_capital_reserves": "BS3 - Share Capital & Reserves",
    "total_equity": "BS3 - Share Capital & Reserves",
    "non_current_liabilities": "BS4 - Non-Current Liabilities",
    "current_liabilities": "BS5 - Current Liabilities",
}

_DEFAULT_THRESHOLD = 88.0


def normalize_label(label: str) -> str:
    """Lowercase, strip encoding artifacts and punctuation noise for matching."""
    if label is None:
        return ""
    s = str(label).lower()
    # drop common mojibake / dashes / leading note markers
    s = s.replace("–", "-").replace("—", "-").replace("�", " ")
    s = re.sub(r"[^a-z0-9&\- ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _load_registry(path: Path) -> dict[str, list[str]]:
    """{canonical_id: [aliases...]} from the shared registry; {} if unavailable."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for cid, meta in raw.items():
        al = list(meta.get("aliases", []))
        dn = meta.get("display_name")
        if dn:
            al.append(dn)
        out[cid] = al
    return out


class MetricOntology:
    """Resolve a raw label to a canonical metric id (or None).

    Sources, in precedence order (highest first):
      1. FIE SEED_ALIASES  — authoritative for the exact workbook/mojibake labels
      2. canonical_metric_registry.json — the shared 419-metric registry (reconciled
         to FIE ids via REGISTRY_TO_FIE)
    """

    def __init__(self, aliases: dict[str, list[str]] | None = None,
                 threshold: float = _DEFAULT_THRESHOLD,
                 registry_path: str | Path | None = _DEFAULT_REGISTRY,
                 use_registry: bool = True) -> None:
        self.threshold = threshold
        self._exact: dict[str, str] = {}
        self._by_canonical: dict[str, list[str]] = {}

        # 1. registry first, with setdefault so FIE seed (next) overrides on conflict
        if use_registry and registry_path is not None:
            for cid, variants in _load_registry(registry_path).items():
                fie_id = REGISTRY_TO_FIE.get(cid, cid)
                self._by_canonical.setdefault(fie_id, [])
                for v in [cid, *variants]:
                    n = normalize_label(v)
                    if n:
                        self._exact.setdefault(n, fie_id)
                        self._by_canonical[fie_id].append(v)

        # 2. FIE seed overrides — authoritative for the labels the FIE depends on
        for canonical, variants in (aliases or SEED_ALIASES).items():
            self._by_canonical.setdefault(canonical, [])
            self._exact[normalize_label(canonical)] = canonical
            for v in variants:
                self._exact[normalize_label(v)] = canonical
                self._by_canonical[canonical].append(v)

        # fixed choice list for fast fuzzy matching (C-optimized rapidfuzz.process)
        self._alias_keys = list(self._exact.keys())

    def canonical(self, label: str, *, sheet: str | None = None) -> Optional[str]:
        """exact alias -> normalized exact -> fuzzy(>=threshold) -> None."""
        norm = normalize_label(label)
        if not norm:
            return None
        if norm in self._exact:
            return self._exact[norm]
        match = process.extractOne(
            norm, self._alias_keys, scorer=fuzz.token_sort_ratio,
            score_cutoff=self.threshold,
        )
        return self._exact[match[0]] if match else None

    def aliases(self, metric: str) -> list[str]:
        return self._by_canonical.get(metric, [])

    def build_query_matcher(
        self, available: set[str]
    ) -> list[tuple[re.Pattern, str]]:
        """Return (pattern, canonical_id) pairs for metrics in *available*.

        Replaces the hardcoded _METRIC_KEYWORDS list in understanding.py with
        a workbook-specific matcher derived from the registry + seed aliases.
        Only metrics actually present in the uploaded workbook are included,
        so the matcher automatically adapts to every different Excel file.

        Aliases are sorted longest-first within each metric so multi-word
        phrases win over single-word fallbacks (e.g. 'gross profit' matched
        before bare 'profit').  Metrics whose longest alias is longest sort
        first overall for the same reason.
        """
        entries: list[tuple[re.Pattern, str, int]] = []
        for metric in available:
            raw_aliases = self._by_canonical.get(metric, [])
            if not raw_aliases:
                continue
            # deduplicate on the normalised key; keep longest form of each
            seen: set[str] = set()
            deduped: list[str] = []
            for alias in sorted(raw_aliases, key=len, reverse=True):
                key = normalize_label(alias)
                if key and key not in seen:
                    seen.add(key)
                    deduped.append(alias)
            if not deduped:
                continue
            parts = [re.escape(a) for a in deduped]
            pattern = re.compile(
                r"\b(?:" + "|".join(parts) + r")\b", re.I
            )
            entries.append((pattern, metric, max(len(a) for a in deduped)))
        # most-specific (longest alias) patterns first
        entries.sort(key=lambda x: x[2], reverse=True)
        matched = [(p, m) for p, m, _ in entries]
        skipped = len(available) - len(matched)
        _log.debug(
            "fie build_query_matcher: %d patterns built, %d metrics skipped (no aliases)",
            len(matched), skipped,
            extra={"component": "Ontology"},
        )
        if skipped:
            no_alias = sorted(available - {m for _, m in matched})
            _log.debug(
                "fie build_query_matcher: metrics with no aliases: %s", no_alias,
                extra={"component": "Ontology"},
            )
        return matched
