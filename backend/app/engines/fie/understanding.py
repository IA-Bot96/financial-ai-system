"""Query understanding (L1) — Phase 1 rule-based builder.

Regex/keyword extraction only; no LLM. Phase 3 adds an LLM builder behind the
same ``QueryFrame`` schema. See docs/fie_implementation_plan.md §Phase 1 (1.2).
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .models import QueryFrame

_log = logging.getLogger("app.engines.fie")

# ---------------------------------------------------------------------------
# Normalisation: map user phrasing to the exact keywords the rules classifier
# expects.  Applied before build_frame() so both intent detection and year
# extraction see the canonical form.  Order matters — more specific first.
# ---------------------------------------------------------------------------
_NORMALIZATIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bgross\s+profit\s+margin\b",       re.I), "gross margin"),
    (re.compile(r"\bnet\s+profit\s+margin\b",         re.I), "net margin"),
    (re.compile(r"\boperating\s+profit\s+margin\b",   re.I), "operating margin"),
    (re.compile(r"\bprofit\s+before\s+tax\s+margin\b",re.I), "net margin"),
    (re.compile(r"\bEBITDA\s+margin\b",               re.I), "EBITDA margin"),
    (re.compile(r"\bfor\s+each\s+year\b",             re.I), "over the years"),
    (re.compile(r"\beach\s+year\b",                   re.I), "over the years"),
    (re.compile(r"\bevery\s+year\b",                  re.I), "over the years"),
    (re.compile(r"\byear\s+by\s+year\b",              re.I), "over the years"),
    (re.compile(r"\bper\s+year\b",                    re.I), "over the years"),
    (re.compile(r"\ball\s+years\b",                   re.I), "over the years"),
    (re.compile(r"\ball\s+financial\s+years\b",       re.I), "over the years"),
    (re.compile(r"\bannually\b",                      re.I), "over the years"),
]


def _normalize_query(query: str) -> str:
    original = query
    for pattern, replacement in _NORMALIZATIONS:
        query = pattern.sub(replacement, query)
    # Expand bare 2-digit year tokens: "25?" → "2025?" — only when the entire
    # query (stripped) is nothing but the short year, so "25 employees" is safe.
    stripped = query.strip()
    m = re.fullmatch(r"([0-2]\d)\??", stripped)
    if m:
        query = f"20{stripped}" if not stripped.endswith("?") else f"20{stripped[:-1]}?"
    if query != original:
        _log.info("fie normalize: %r -> %r", original, query,
                  extra={"component": "Understand"})
    return query


# ---------------------------------------------------------------------------
# Follow-up detection & LLM-based expansion (uses conversation history)
# ---------------------------------------------------------------------------
_FOLLOW_UP_RE = re.compile(
    r"^\s*(?:and|also|how about|what about|same for|for that|ok but|but what|"
    r"in that|what was it|how was it|for those|\d{4}\??)"
    r"[\s,?]",
    re.IGNORECASE,
)


def _is_follow_up(query: str) -> bool:
    """True when the query is too ambiguous to classify without conversation context."""
    q = query.strip()
    if re.fullmatch(r"\d{4}\??", q):        # bare year: "2025" or "2025?"
        return True
    if _FOLLOW_UP_RE.match(q):              # starts with follow-up marker
        return True
    if len(q) <= 25:                        # very short — no subject noun
        return True
    return False


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
    # bare single-word fallbacks LAST so more specific two-word patterns above win first
    (re.compile(r"\bliabilities\b", re.I), "current_liabilities"),
    (re.compile(r"\bassets\b",      re.I), "total_assets"),
    (re.compile(r"\bequity\b",      re.I), "total_equity"),
]

# bare/unqualified "profit"/"earnings" — ambiguous metric (gross/operating/net),
# routed to metric_lookup so the availability gate can clarify.
_AMBIGUOUS_METRIC_RE = re.compile(r"\bprofit(s|ability)?\b|\bearnings\b", re.I)

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
_DIVIDEND_RE = re.compile(r"dividend|payout|book closure|bonus issue", re.I)
_EARNINGS_RE = re.compile(r"earnings review|review (the )?earnings|latest results", re.I)


def _matched_formula(query: str):
    for pattern, formula_id, metrics in _FORMULA_KEYWORDS:
        if pattern.search(query):
            return formula_id, list(metrics)
    return None, []


def _matched_metric(query: str, matcher=None) -> Optional[str]:
    """Return the first canonical metric id whose pattern matches query.

    Uses *matcher* (workbook-specific, built from the ontology) when supplied;
    falls back to the hardcoded _METRIC_KEYWORDS for backwards-compat / tests.
    """
    source = matcher if matcher is not None else _METRIC_KEYWORDS
    for pattern, metric in source:
        if pattern.search(query):
            return metric
    return None


def build_frame(query: str, metric_matcher=None) -> QueryFrame:
    """Extract a QueryFrame from a natural-language query (rules only).

    *metric_matcher* is a (pattern, canonical_id) list built from the ontology
    for the metrics actually present in the uploaded workbook.  When supplied it
    replaces the hardcoded _METRIC_KEYWORDS, making metric recognition workbook-
    specific without any manual list maintenance.  Falls back to _METRIC_KEYWORDS
    when None (tests, direct callers without a store).
    """
    company = _extract_company(query)
    companies = _extract_companies(query)
    year = _extract_year(query)

    # peer comparison: two companies, or an explicit compare/vs cue
    if len(companies) >= 2 or (_PEER_RE.search(query) and companies):
        formula_id, _ = _matched_formula(query)
        m = _matched_metric(query, metric_matcher)
        return QueryFrame(
            raw_query=query, intent="peer_comparison", company=companies[0],
            companies=companies, year=year, formula=formula_id,
            metrics=([m] if (not formula_id and m) else []),
        )

    # valuation (needs market data)
    if _VALUATION_RE.search(query):
        return QueryFrame(raw_query=query, intent="valuation", company=company, year=year,
                          formula="pe_ratio")

    # forecast validation
    if _FORECAST_RE.search(query):
        return QueryFrame(raw_query=query, intent="forecast_validation", company=company,
                          year=year, metrics=[_matched_metric(query, metric_matcher) or "revenue"])

    if _EARNINGS_RE.search(query):
        return QueryFrame(raw_query=query, intent="earnings_review", company=company, year=year)

    if _NEWS_RE.search(query):
        return QueryFrame(raw_query=query, intent="news_impact", company=company, year=year)

    if _DIVIDEND_RE.search(query):
        return QueryFrame(raw_query=query, intent="dividend_analysis", company=company, year=year)

    # trend / multi-year: explicit range, a "last N years" window, an aggregation
    # operator ("average increase/growth"), or trend keywords
    years, window = _extract_period(query)
    metric = _matched_metric(query, metric_matcher)
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

    # metric lookup: workbook-specific matcher first, hardcoded fallback if no matcher
    _metric_source = metric_matcher if metric_matcher is not None else _METRIC_KEYWORDS
    for pattern, mid in _metric_source:
        if pattern.search(query):
            return QueryFrame(raw_query=query, intent="metric_lookup",
                              company=company, year=year, metrics=[mid])

    # an unqualified "profit"/"earnings" value question is an ambiguous metric_lookup
    # (no canonical chosen) — the engine's availability gate decides clarify vs resolve.
    if _AMBIGUOUS_METRIC_RE.search(query):
        _log.debug("fie build_frame: ambiguous_metric -> metric_lookup (no canonical) query=%r", query,
                   extra={"component": "Understand"})
        return QueryFrame(raw_query=query, intent="metric_lookup",
                          company=company, year=year, metrics=[])

    _log.debug("fie build_frame: no branch matched -> unknown query=%r", query,
               extra={"component": "Understand"})
    return QueryFrame(raw_query=query, intent="unknown", company=company, year=year)


# --- LLM fallback (3.1): rules stay the high-precision first pass ----------

_SUPPORTED_INTENTS = {"ratio_analysis", "metric_lookup", "risk_assessment"}
_KNOWN_FORMULAS = {f for _, f, _ in _FORMULA_KEYWORDS}

_ALL_INTENTS = {
    "peer_comparison", "valuation", "forecast_validation", "earnings_review",
    "news_impact", "dividend_analysis", "trend_analysis", "ratio_analysis",
    "risk_assessment", "metric_lookup", "unknown",
}

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

_VALIDATE_SYS = (
    "You are validating a financial query classification produced by a rules engine. "
    "You receive: the current query, full conversation history (user questions AND "
    "assistant answers — use these to resolve references like 'same year', 'same metric', "
    "'expenses?' from the actual prior answers), the rules-based classification, and the "
    "workbook's available metrics and years. "
    "Use conversation history to understand what the user is asking when the query is "
    "ambiguous or a follow-up. Use workbook context to constrain valid values. "
    "Supported intents: peer_comparison, valuation, forecast_validation, earnings_review, "
    "news_impact, dividend_analysis, trend_analysis, ratio_analysis, risk_assessment, "
    "metric_lookup, unknown. "
    "Only use metric IDs from available_metrics — never invent new ones. "
    "Only use years from available_years. "
    "Return JSON with keys: intent (string), year (int or null), "
    "formula (known formula id or null), metrics (list of metric ids from available_metrics). "
    "If the rules classification is already correct, return the same values unchanged."
)

_VALIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent":  {"type": "string"},
        "year":    {"type": ["integer", "null"]},
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


def validate_frame_llm(
    query: str,
    rules_frame: QueryFrame,
    llm,
    available_metrics: Optional[list[str]] = None,
    available_years: Optional[list[int]] = None,
    history: Optional[list[dict]] = None,
) -> Optional[QueryFrame]:
    """Single GPT call that expands follow-ups AND validates the rules frame.

    Receives query + conversation history + rules frame + workbook context in one
    prompt — GPT resolves ambiguous/follow-up queries using history, then confirms
    or corrects the intent, year, metrics, and formula using workbook context.
    Falls back to the rules frame on any failure.
    """
    if llm is None:
        return None

    # Format history for GPT context:
    # - User turns: verbatim query text (needed for follow-up markers like "and what about...")
    # - Assistant turns: resolved frame only (intent/year/metrics/formula) — not prose.
    #   The prose answer is long, noisy, and irrelevant for intent resolution. GPT only
    #   needs to know WHAT was resolved to handle "2025?", "same metric?", "and margin?".
    recent_turns: list[str] = []
    for t in (history or [])[-16:]:   # last 16 messages = last 8 full turns (frames are compact)
        role = t.get("role", "")
        text = (t.get("text") or "").strip()
        frame_dict = t.get("frame")  # present for server-side stored assistant turns

        if role == "user":
            if text:
                recent_turns.append(f"  User: {text}")
        elif role == "assistant":
            if frame_dict and frame_dict.get("intent"):
                parts = [f"intent={frame_dict['intent']}"]
                if frame_dict.get("year") is not None:
                    parts.append(f"year={frame_dict['year']}")
                if frame_dict.get("metrics"):
                    parts.append(f"metrics={frame_dict['metrics']}")
                if frame_dict.get("formula"):
                    parts.append(f"formula={frame_dict['formula']}")
                recent_turns.append(f"  Assistant [Resolved: {', '.join(parts)}]")
            elif text:
                recent_turns.append(f"  Assistant: {text}")  # fallback: no frame stored

    context_lines = [f"Current query: {query}"]
    if recent_turns:
        context_lines.append("Conversation history:\n" + "\n".join(recent_turns))
    context_lines.append(
        f"Rules classification: intent={rules_frame.intent!r}, "
        f"year={rules_frame.year}, metrics={rules_frame.metrics}, "
        f"formula={rules_frame.formula!r}"
    )
    if available_metrics:
        context_lines.append(f"Available metrics in workbook: {sorted(available_metrics)}")
    if available_years:
        context_lines.append(f"Available years in workbook: {sorted(available_years)}")

    _log.debug(
        "fie LLM validate: query=%r rules_intent=%r year=%s metrics=%s "
        "turns=%d available_years=%s",
        query, rules_frame.intent, rules_frame.year, rules_frame.metrics,
        len(recent_turns), available_years,
        extra={"component": "Understand"},
    )

    data = llm.complete_json(_VALIDATE_SYS, "\n".join(context_lines), _VALIDATE_SCHEMA)
    if not isinstance(data, dict):
        _log.warning("fie LLM validate: invalid response (not a dict) for query=%r", query,
                     extra={"component": "Understand"})
        return None

    intent = data.get("intent")
    if intent not in _ALL_INTENTS:
        _log.warning("fie LLM validate: unknown intent %r returned for query=%r — keeping rules",
                     intent, query, extra={"component": "Understand"})
        return None

    formula = data.get("formula")
    if formula is not None and formula not in _KNOWN_FORMULAS:
        _log.debug("fie LLM validate: unknown formula %r dropped", formula,
                   extra={"component": "Understand"})
        formula = None

    # Restrict returned metrics to what the workbook actually contains.
    raw_metrics = [m for m in (data.get("metrics") or []) if isinstance(m, str)]
    metrics = (
        [m for m in raw_metrics if m in available_metrics]
        if available_metrics else raw_metrics
    )
    dropped = set(raw_metrics) - set(metrics)
    if dropped:
        _log.debug("fie LLM validate: hallucinated metrics dropped: %s", dropped,
                   extra={"component": "Understand"})

    llm_year = data.get("year") or rules_frame.year

    # Log whether the LLM corrected anything or just confirmed the rules frame.
    if (intent != rules_frame.intent
            or metrics != rules_frame.metrics
            or llm_year != rules_frame.year):
        _log.info(
            "fie LLM correction: intent %r->%r  year %s->%s  metrics %s->%s",
            rules_frame.intent, intent,
            rules_frame.year, llm_year,
            rules_frame.metrics, metrics,
            extra={"component": "Understand"},
        )
    else:
        _log.debug("fie LLM validate: rules frame confirmed (intent=%r year=%s metrics=%s)",
                   intent, llm_year, metrics, extra={"component": "Understand"})

    try:
        return QueryFrame(
            raw_query=query,
            intent=intent,
            company=rules_frame.company or _extract_company(query),
            companies=rules_frame.companies,
            year=llm_year,
            years=rules_frame.years,
            window=rules_frame.window,
            aggregation=rules_frame.aggregation,
            formula=formula,
            metrics=metrics,
            level=rules_frame.level,
            period_type=rules_frame.period_type,
            source="llm",
        )
    except Exception as exc:
        _log.warning("fie LLM validate: QueryFrame construction failed: %s", exc,
                     extra={"component": "Understand"})
        return None


def understand(
    query: str,
    llm=None,
    available_metrics: Optional[list[str]] = None,
    available_years: Optional[list[int]] = None,
    history: Optional[list[dict]] = None,
    metric_matcher=None,
) -> QueryFrame:
    """Full understanding pipeline — all steps run inside the engine:

    1. Normalise     — map user phrasing to canonical engine keywords
    2. Rules         — fast regex classifier; uses *metric_matcher* (workbook-
                       specific, built from the ontology) for metric recognition
                       when supplied, falling back to the hardcoded list otherwise
    3. ONE GPT call  — receives query + conversation history + rules frame +
                       workbook context; resolves follow-ups, confirms or
                       corrects the intent, year, metrics, and formula
    """
    # Step 1: normalise
    query = _normalize_query(query)

    if history and _is_follow_up(query):
        _log.info("fie understand: follow-up detected: %r (history_len=%d)",
                  query, len(history), extra={"component": "Understand"})

    # Step 2: rules-based classification (workbook-specific matcher when available)
    matcher_label = f"workbook ({len(metric_matcher)} patterns)" if metric_matcher is not None else "fallback-keywords"
    _log.debug("fie understand: metric_matcher=%s", matcher_label,
               extra={"component": "Understand"})
    frame = build_frame(query, metric_matcher)
    _log.info(
        "fie rules frame: intent=%r year=%s metrics=%s formula=%r",
        frame.intent, frame.year, frame.metrics, frame.formula,
        extra={"component": "Understand"},
    )

    # Step 3: single GPT call — expand + validate in one shot
    if llm is not None:
        validated = validate_frame_llm(
            query, frame, llm, available_metrics, available_years, history
        )
        if validated is not None:
            _log.info(
                "fie understand final: intent=%r year=%s metrics=%s formula=%r source=%r",
                validated.intent, validated.year, validated.metrics,
                validated.formula, validated.source,
                extra={"component": "Understand"},
            )
            return validated
        _log.info("fie LLM validation returned None — using rules frame (intent=%r)",
                  frame.intent, extra={"component": "Understand"})
    else:
        _log.debug("fie understand: no LLM configured, using rules frame only",
                   extra={"component": "Understand"})

    return frame
