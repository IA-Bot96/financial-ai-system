"""Query understanding (L1) — Phase 1 rule-based builder.

Regex/keyword extraction only; no LLM. Phase 3 adds an LLM builder behind the
same ``QueryFrame`` schema. See docs/fie_implementation_plan.md §Phase 1 (1.2).
"""

from __future__ import annotations

import re
from typing import Optional

from .models import QueryFrame

# formula keyword -> (canonical formula id, required metrics)
_FORMULA_KEYWORDS: list[tuple[re.Pattern, str, list[str]]] = [
    (re.compile(r"current\s+ratio", re.I), "current_ratio",
     ["current_assets", "current_liabilities"]),
    (re.compile(r"quick\s+ratio|acid[- ]test", re.I), "quick_ratio",
     ["current_assets", "stock_in_trade", "current_liabilities"]),
    (re.compile(r"gross\s+margin", re.I), "gross_margin", ["gross_profit", "revenue"]),
    (re.compile(r"operating\s+margin", re.I), "operating_margin",
     ["operating_profit", "revenue"]),
    (re.compile(r"net\s+margin|net\s+profit\s+margin", re.I), "net_margin",
     ["pat", "revenue"]),
    (re.compile(r"\broe\b|return on equity", re.I), "roe", ["pat", "total_equity"]),
    (re.compile(r"\broa\b|return on assets", re.I), "roa", ["pat", "total_assets"]),
    (re.compile(r"debt[- ]to[- ]equity|d/e\b|gearing", re.I), "debt_to_equity",
     ["non_current_liabilities", "current_liabilities", "total_equity"]),
    (re.compile(r"interest\s+coverage|times interest earned", re.I), "interest_coverage",
     ["operating_profit", "finance_cost"]),
    (re.compile(r"revenue\s+growth|sales\s+growth", re.I), "revenue_growth", ["revenue"]),
    (re.compile(r"earnings\s+growth|profit\s+growth", re.I), "earnings_growth", ["pat"]),
    (re.compile(r"free\s+cash\s+flow|\bfcf\b", re.I), "free_cash_flow",
     ["operating_cash_flow", "capex"]),
    (re.compile(r"\bebitda\b", re.I), "ebitda", ["operating_profit", "depreciation_expense"]),
    (re.compile(r"book\s+value\s+per\s+share|\bbvps\b", re.I), "book_value_per_share",
     ["total_equity", "shares_outstanding"]),
]

_RISK_RE = re.compile(r"\brisk", re.I)
# metric keyword -> canonical id (for direct value lookups)
_METRIC_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brevenue|net sales|turnover", re.I), "revenue"),
    (re.compile(r"profit after tax|net (income|profit)|\bpat\b|earnings", re.I), "pat"),
    (re.compile(r"gross profit", re.I), "gross_profit"),
    (re.compile(r"operating profit", re.I), "operating_profit"),
    (re.compile(r"total assets", re.I), "total_assets"),
    (re.compile(r"total equity|shareholders.? equity", re.I), "total_equity"),
    (re.compile(r"current assets", re.I), "current_assets"),
    (re.compile(r"current liabilities", re.I), "current_liabilities"),
    # bare "assets"/"equity" LAST so the more specific patterns above win first
    (re.compile(r"\bassets\b", re.I), "total_assets"),
    (re.compile(r"\bequity\b", re.I), "total_equity"),
]

# aggregation operators over a multi-year series
_AGG_RE = re.compile(
    r"average\s+(?:annual\s+)?(?:increase|growth|change|rise|gain)|"
    r"avg\s+(?:increase|growth|change)|mean\s+(?:increase|growth|change)|\bcagr\b",
    re.I,
)

# company alias -> canonical company name (matches manifest company strings loosely)
_COMPANY_ALIASES = {
    "mtl": "Millat Tractors Limited",
    "millat": "Millat Tractors Limited",
    "luck": "Lucky Cement Limited",
    "lucky": "Lucky Cement Limited",
}

# canonical company name -> PSX ticker
COMPANY_TICKER = {
    "Millat Tractors Limited": "MTL",
    "Lucky Cement Limited": "LUCK",
}

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_RANGE_RE = re.compile(r"\b(19|20)\d{2}\b\s*(?:-|to|through|–|until|and)\s*\b(19|20)\d{2}\b", re.I)
_WINDOW_RE = re.compile(r"\b(?:last|past)\s+(\d{1,2})\s+years?|\b(\d{1,2})[- ]year\b", re.I)
_TREND_RE = re.compile(r"\btrend|over the years|historical|history|year[- ]over[- ]year|yoy|trajectory", re.I)


def _extract_company(q: str) -> Optional[str]:
    found = _extract_companies(q)
    return found[0] if found else None


def _extract_companies(q: str) -> list[str]:
    ql = q.lower()
    out: list[str] = []
    for alias, name in _COMPANY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", ql) and name not in out:
            out.append(name)
    return out


def _extract_year(q: str) -> Optional[int]:
    m = _YEAR_RE.search(q)
    return int(m.group(0)) if m else None


def _extract_period(q: str) -> tuple[list[int], Optional[int]]:
    """Return (explicit_years, window). explicit_years = inclusive range if given;
    window = N for 'last N years' / 'N-year' (resolved against the store later)."""
    rng = _RANGE_RE.search(q)
    if rng:
        a, b = sorted(int(y) for y in re.findall(r"(?:19|20)\d{2}", rng.group(0)))
        return list(range(a, b + 1)), None
    win = _WINDOW_RE.search(q)
    if win:
        n = int(win.group(1) or win.group(2))
        return [], n
    return [], None


_PEER_RE = re.compile(r"\bvs\.?\b|versus|compare|against|peer", re.I)
_VALUATION_RE = re.compile(
    r"\bp/?e\b|pe ratio|price[- ]to[- ]earnings|price[- ]to[- ]book|\bp/?b\b|"
    r"ev/?ebitda|enterprise value|valuation|how cheap|expensive", re.I)
_FORECAST_RE = re.compile(r"forecast|guidance|on track|remain(ed)? valid|projection", re.I)
_NEWS_RE = re.compile(r"\bnews\b|headlines|announcements?", re.I)
_EARNINGS_RE = re.compile(r"earnings review|review (the )?earnings|latest results", re.I)


def _matched_formula(query: str):
    for pattern, formula_id, metrics in _FORMULA_KEYWORDS:
        if pattern.search(query):
            return formula_id, list(metrics)
    return None, []


def _matched_metric(query: str) -> Optional[str]:
    for pattern, metric in _METRIC_KEYWORDS:
        if pattern.search(query):
            return metric
    return None


def build_frame(query: str) -> QueryFrame:
    """Extract a QueryFrame from a natural-language query (rules only)."""
    company = _extract_company(query)
    companies = _extract_companies(query)
    year = _extract_year(query)

    # peer comparison: two companies, or an explicit compare/vs cue
    if len(companies) >= 2 or (_PEER_RE.search(query) and companies):
        formula_id, _ = _matched_formula(query)
        return QueryFrame(
            raw_query=query, intent="peer_comparison", company=companies[0],
            companies=companies, year=year, formula=formula_id,
            metrics=([_matched_metric(query)] if (not formula_id and _matched_metric(query)) else []),
        )

    # valuation (needs market data)
    if _VALUATION_RE.search(query):
        return QueryFrame(raw_query=query, intent="valuation", company=company, year=year,
                          formula="pe_ratio")

    # forecast validation
    if _FORECAST_RE.search(query):
        return QueryFrame(raw_query=query, intent="forecast_validation", company=company,
                          year=year, metrics=[_matched_metric(query) or "revenue"])

    if _EARNINGS_RE.search(query):
        return QueryFrame(raw_query=query, intent="earnings_review", company=company, year=year)

    if _NEWS_RE.search(query):
        return QueryFrame(raw_query=query, intent="news_impact", company=company, year=year)

    # trend / multi-year: explicit range, a "last N years" window, an aggregation
    # operator ("average increase/growth"), or trend keywords
    years, window = _extract_period(query)
    metric = _matched_metric(query)
    agg_match = _AGG_RE.search(query)
    if (years or window or agg_match or _TREND_RE.search(query)) and metric:
        aggregation = None
        if agg_match:
            aggregation = "cagr" if "cagr" in agg_match.group(0).lower() else "average"
        return QueryFrame(raw_query=query, intent="trend_analysis", company=company,
                          year=year, years=years, window=window, metrics=[metric],
                          aggregation=aggregation)

    for pattern, formula_id, metrics in _FORMULA_KEYWORDS:
        if pattern.search(query):
            return QueryFrame(
                raw_query=query, intent="ratio_analysis", company=company,
                year=year, formula=formula_id, metrics=list(metrics),
            )

    if _RISK_RE.search(query):
        return QueryFrame(raw_query=query, intent="risk_assessment",
                          company=company, year=year)

    for pattern, metric in _METRIC_KEYWORDS:
        if pattern.search(query):
            return QueryFrame(raw_query=query, intent="metric_lookup",
                              company=company, year=year, metrics=[metric])

    return QueryFrame(raw_query=query, intent="unknown", company=company, year=year)


# --- LLM fallback (3.1): rules stay the high-precision first pass ----------

_SUPPORTED_INTENTS = {"ratio_analysis", "metric_lookup", "risk_assessment"}
_KNOWN_FORMULAS = {f for _, f, _ in _FORMULA_KEYWORDS}

_LLM_SYS = (
    "Extract a structured query frame for a financial Q&A engine. "
    "Respond as JSON with keys: intent (one of ratio_analysis, metric_lookup, "
    "risk_assessment, unknown), company, year (int or null), formula (a known id or "
    "null), metrics (list of canonical metric ids). Do not invent values."
)

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "company": {"type": ["string", "null"]},
        "year": {"type": ["integer", "null"]},
        "formula": {"type": ["string", "null"]},
        "metrics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent"],
}


def build_frame_llm(query: str, llm) -> Optional[QueryFrame]:
    """LLM-backed frame builder constrained to the QueryFrame schema. Returns a
    validated frame for a SUPPORTED intent, else None (so the caller keeps rules)."""
    if llm is None:
        return None
    data = llm.complete_json(_LLM_SYS, query, _LLM_SCHEMA)
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent not in _SUPPORTED_INTENTS:
        return None
    formula = data.get("formula")
    if formula is not None and formula not in _KNOWN_FORMULAS:
        formula = None  # never trust an unknown formula id from the model
    try:
        return QueryFrame(
            raw_query=query, intent=intent,
            company=(_extract_company(query) or data.get("company")),
            year=data.get("year"), formula=formula,
            metrics=[m for m in (data.get("metrics") or []) if isinstance(m, str)],
            source="llm",
        )
    except Exception:
        return None


def understand(query: str, llm=None) -> QueryFrame:
    """Rules first (high precision); LLM only when rules can't classify."""
    frame = build_frame(query)
    if frame.intent == "unknown" and llm is not None:
        llm_frame = build_frame_llm(query, llm)
        if llm_frame is not None:
            return llm_frame
    return frame
