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

# Qualitative / MD&A-style questions answered from the workbook's narrative insights.
# risk_assessment is the engine's qualitative-insights handler, so it owns all of these.
# This branch sits AFTER every quantitative branch in build_frame, so a broad match here
# can only catch queries that fell through metric/ratio/trend/etc. detection.
_RISK_RE = re.compile(
    r"\brisk|\bchalleng|\bheadwind|\bconcern|\bpressure|\bexposure|\buncertaint"
    r"|\bmanagement('?s)? (say|view|comment|discuss|expect|believe|note|outlook|guidance)"
    r"|\b(operational|business|industry|market|economic|macro|competitive|regulatory) "
    r"(condition|challenge|issue|environment|outlook|landscape|dynamic|factor)"
    r"|\bmarket share|\bdemand\b|\bcompetiti|\bstrateg|\bstrength|\bweakness"
    r"|\bopportunit|\boutlook\b|\bguidance\b",
    re.I,
)
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

# Edit-history: questions about CHANGES THE USER MADE to the workbook via the app (not about
# how a financial metric moved over years). Requires first-person authorship ("I made / my
# changes / did I change") or an app-state cue ("unsaved", "this session") so "why did revenue
# change" / "my revenue change over the years" stay financial. The LLM validator is a backstop.
_EDIT_HISTORY_RE = re.compile(
    r"\bunsaved\b"
    r"|\bin (this|the current|my) session\b"
    # authorship + change word (either order); allow up to 2 words between (e.g. "I have made",
    # "did I just make") so "which sheet I have made most changes" / "how many changes did I make"
    # are caught. Requires first-person authorship, so data queries about "changes" don't match.
    r"|\b(change|edit|modification|update)s?\b[^.?]{0,40}\b(i (?:\w+ ){0,2}(made|did|make)|did i (?:\w+ ){0,2}(make|do))\b"
    r"|\b(i (?:\w+ ){0,2}(made|did|make)|did i (?:\w+ ){0,2}(make|do))\b[^.?]{0,40}\b(change|edit|modification|update)s?\b"
    r"|\bmy (last |recent |latest )?(\d+ )?(unsaved )?(change|edit|modification|update)s?\b"
    r"|\bwhat did i (change|edit|update|modify)\b"
    # manual-verification markers ("which cells did I mark as manually verified") — the MV toggles
    # live in the edit log (Validation Ledger), so this is an edit-history ask, not a data question.
    r"|\bmanually verified\b|\bmark\w*\b[^.?]{0,20}\bverif\w*\b|\bverified cells?\b"
    # "which sheet did I change/edit (most)" — first-person, so data queries don't match
    r"|\bwhich sheets?\b[^.?]{0,30}\b(did i|i)\b[^.?]{0,20}\b(chang\w*|edit\w*|made|make|updat\w*|modif\w*)\b"
    # workbook lifecycle: "when was this workbook opened/loaded" (tight — requires the
    # open/load/upload verb adjacent to workbook/file/session so financial queries don't match)
    r"|\b(open(ed)?|load(ed)?|upload(ed)?)\b[^.?]{0,30}\b(workbook|file|session|excel|spreadsheet)\b"
    r"|\b(workbook|file|session|excel|spreadsheet)\b[^.?]{0,30}\b(open(ed)?|load(ed)?|uploaded)\b",
    re.I)

# Ad-hoc / unregistered computations -> the agent (compute_expr), which evaluates the expression
# over metric ids and registers the result. Triggered by an explicit arithmetic expression after
# "calculate/compute" (so "calculate net margin" / "calculate EBITDA" stay on their ratio paths),
# or by a named ratio the formula registry doesn't carry (ROIC, ROCE, cash conversion cycle, …).
_ADHOC_CALC_RE = re.compile(
    r"\b(calculate|compute|work out)\b[^?]*\b(divided by|multiplied by)\b"
    r"|\b(calculate|compute|work out)\b[^?]*[=/]",
    re.I)
_ADHOC_METRIC_RE = re.compile(
    r"\b(roic|roce|nopat|cash conversion cycle|free cash flow|fcf)\b", re.I)

# "X as a percentage / share / fraction of Y" — a single value PLUS its ratio (metric_lookup +
# the percentage path), NOT a two-series comparison.
_PCT_OF_RE = re.compile(
    r"\b(percent(age)?|share|fraction|proportion)\s+of\b|%\s*of\b|\bas a (?:%|percent\w*|share)\b", re.I)

# --------------------------------------------------------------------------------------------
# Workbook-METADATA / availability ("does this workbook have cash-flow data?", "what years are
# covered?", "what company is this?"). Answered deterministically from the store's metrics /
# sheets / years / company — never a value lookup or the agent.
#
# CRITICAL — this is a RULES-ONLY intent. The LLM validator is deliberately NOT told it exists
# (see understand(): the rules frame is kept as-is when it fires). That is what makes it safe:
# the ONLY path into data_availability is these two narrow, anchored regexes, so the LLM can
# never pull a value/ratio/trend/validation/qualitative query into it. Keep them TIGHT.
# --------------------------------------------------------------------------------------------
_AVAILABILITY_RE = re.compile(
    # "does this workbook have / contain / is there ... <statement family | data | metric | figures>"
    r"\b(do|does|is|are|has|have)\b[^?]{0,30}\b(have|contain|include|got|there|any)\b"
    r"[^?]{0,30}\b(cash[\s-]?flow|cashflow|balance[\s-]?sheet|income statement|p&l|"
    r"profit and loss|(statement of )?changes in equity|data|metrics?|figures?)\b"
    # "is there a <statement family>"
    r"|\bis there (any |a |an )?(cash[\s-]?flow|balance[\s-]?sheet|income statement|p&l|"
    r"profit and loss|(statement of )?changes in equity)\b"
    # "what/which <data|sheets|statements|metrics|years> ARE available / in this workbook" — the
    # keyword must sit DIRECTLY after what/which, so the definitional "what IS <name> SHEET ..."
    # (e.g. 'what is edit history sheet in this workbook') does NOT match.
    r"|\b(what|which)\s+(financial\s+)?(years?|data|sheets?|statements?|metrics?)\b"
    r"[^?]{0,25}\b(available|covered|present|included|in (this|the) (workbook|file|model)|"
    r"does (this|the|it)|are there|exist|do (we|you) have)\b"
    # bare coverage asks
    r"|\bwhat\s+years?\b"
    r"|\bwhat(?:'s| is| are)?\s+in (this|the) (workbook|file|model)\b"
    # 'which financial statements'; counts ('how many / total / number of sheets|metrics|…')
    r"|\bwhich financial statements?\b"
    r"|\b(how many|total|number of|count of)\s+(metrics?|sheets?|tabs?|statements?|line items?|years?)\b"
    # specific-sheet INDEX / position: 'index of sheet X', 'sheet X is on what index'
    r"|\b(index|position)\b[^?]{0,25}\bsheets?\b|\bsheets?\b[^?]{0,25}\b(index|position)\b"
    # specific-year membership: 'is 2020 included?', 'does it cover 2026?' — a coverage word must
    # sit NEAR the year so a comparison ('is 2024 revenue > 2023?') is NOT caught
    r"|\b(includ|cover|present|contain|have|got|reported?|available)\w*\b[^?]{0,25}\b(19|20)\d{2}\b"
    r"|\b(19|20)\d{2}\b[^?]{0,25}\b(includ|cover|present|contain|available|reported?)\w*",
    re.I)

# Company / entity identity of the loaded workbook. Anchored so the POSSESSIVE "the company's
# <metric>" (a value question) and "business risks / challenges" (qualitative) do NOT trip it.
_COMPANY_ID_RE = re.compile(
    r"\b(what|which)\s+(company|entity|firm|issuer)\b[^?]{0,25}"
    r"\b(is|are|does|do|this|workbook|file|report|belong)\b"
    r"|\bname of (the |this )?(company|entity|firm|issuer|business)\b"
    r"|\bwhose\b[^?]{0,30}\b(financ|statement|workbook|report|book)\w*",
    re.I)

# A follow-up that is JUST a number/percentage/year ("1000%?", "25%", "2025?", "23?") — a
# YEAR or ASSUMPTION swap on the immediately-preceding forecast/projection/calc question.
_FOLLOWUP_NUM_RE = re.compile(
    r"^(?:what about|how about|and|or|try|with|using|in|for)?\s*[-+]?\d[\d,]*(?:\.\d+)?\s*%?\s*\??$", re.I)


def _last_followup_context(history):
    """If the MOST RECENT prior question was a forecast/projection/ad-hoc calc, return its
    RESOLVED frame dict (carries the expanded raw_query, so chained follow-ups keep context);
    else None. A bare follow-up only applies to the immediately-preceding such question."""
    for i in range(len(history) - 1, -1, -1):
        turn = history[i] or {}
        if turn.get("role") == "assistant" and isinstance(turn.get("frame"), dict):
            fr = turn["frame"]
            return fr if fr.get("intent") in ("forecast_validation", "agent") else None
    return None


def _swap_assumption(basis: str, followup: str) -> str:
    """Rebuild the prior question with its percentage replaced by the follow-up's number."""
    nm = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", followup or "")
    if not nm:
        return basis or followup
    new_pct = nm.group(0).replace(",", "") + "%"
    if not basis:
        return f"assume {new_pct} growth"
    swapped, n = re.subn(r"[-+]?\d[\d,]*(?:\.\d+)?\s*%", new_pct, basis, count=1)
    return swapped if n else f"{basis} (assume {new_pct})"


def _swap_year(basis: str, yr: int) -> str:
    """Rebuild the prior question with its (first) 4-digit year replaced by yr."""
    if not basis:
        return f"for {yr}"
    swapped, n = re.subn(r"\b(?:19|20)\d{2}\b", str(yr), basis, count=1)
    return swapped if n else f"{basis} for {yr}"


def _resolve_followup(result, query, history, available_years=None):
    """Resolve a bare follow-up that is just a YEAR ('2025?', '23?') or an ASSUMPTION ('1000%?',
    '25%') after a forecast/projection/calc: re-run THAT question with the year or assumption
    swapped (against its resolved query, so chains keep context) instead of losing it. Works even
    when the LLM left it unresolved."""
    q = (query or "").strip()
    if not (history and _FOLLOWUP_NUM_RE.match(q)):
        return result
    prior = _last_followup_context(history)
    if not prior:
        return result
    basis = prior.get("raw_query") or ""   # the RESOLVED prior question (carries context)
    nm = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", q)
    if not nm:
        return result
    try:
        v = float(nm.group(0).replace(",", ""))
    except ValueError:
        return result
    is_pct = "%" in q
    prior_has_pct = "%" in basis
    years = set(available_years or [])
    # YEAR follow-up: an explicit 4-digit year, OR a 2-digit available year when the prior
    # question carried NO percentage (a calc like ROIC — a bare number means "for that year").
    yr = None
    if not is_pct:
        if v.is_integer() and 1900 <= v <= 2100:
            yr = int(v)
        elif v.is_integer() and 0 <= v <= 99 and not prior_has_pct:
            cand = 2000 + int(v)
            if not years or cand in years:
                yr = cand
    try:
        carried_intent = (result.intent if result.intent in ("forecast_validation", "agent")
                          else prior.get("intent"))
        common = {
            "intent": carried_intent,
            "metrics": result.metrics or prior.get("metrics") or [],
            "formula": result.formula or prior.get("formula"),
            "source": "llm",
        }
        if yr is not None:
            return result.model_copy(update={**common, "raw_query": _swap_year(basis, yr), "year": yr})
        return result.model_copy(update={**common, "raw_query": _swap_assumption(basis, q),
                                         "year": result.year or prior.get("year")})
    except Exception as exc:  # noqa: BLE001
        _log.warning("fie follow-up resolve failed: %s", exc, extra={"component": "Understand"})
        return result


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
# Comparison of two METRICS/series within one company (not peer companies): split the query
# on the connector, then resolve each side to a metric (and a growth flag).
_COMPARE_SPLIT = re.compile(r"\b(?:versus|vs\.?|against|compared?\s+(?:to|with|against))\b", re.I)
_GROWTH_RE = re.compile(r"\b(growth|change|increase|decline|grew|rose|fell)\b", re.I)


def _comparison_terms(query: str, matcher=None) -> list[tuple[str, str, bool]]:
    """Split a 'A vs B' query into comparable terms -> [(label, metric_id, is_growth)].

    Strips the leading verb ('compare …') and a trailing time phrase ('… across all years'),
    splits on the comparison connector, and resolves each side to a workbook metric. A side
    that mentions growth/change is flagged so the handler computes YoY growth, not the level.
    """
    q = re.sub(r"^\s*\W*(?:compare|contrast|calculate|show(?:\s+me)?|what(?:'s| is)?)\b",
               "", query, flags=re.I)
    q = re.sub(r"\b(?:across|over|for|during|between)\b[\s\S]*$", "", q, flags=re.I)
    out: list[tuple[str, str, bool]] = []
    for part in _COMPARE_SPLIT.split(q):
        part = part.strip()
        if not part:
            continue
        mid = _matched_metric(part, matcher)
        if mid:
            out.append((part, mid, bool(_GROWTH_RE.search(part))))
    return out
_VALUATION_RE = re.compile(
    r"\bp/?e\b|pe ratio|price[- ]to[- ]earnings|price[- ]to[- ]book|\bp/?b\b|"
    r"ev/?ebitda|enterprise value|valuation|how cheap|expensive", re.I)
_FORECAST_RE = re.compile(r"forecast|guidance|on track|remain(ed)? valid|projection", re.I)
# data-validation / balance-sheet audit. High-precision so normal queries ("show the balance
# sheet", "cash balance", "current assets") are NOT caught — needs an explicit audit verb, the
# specific 'balance sheet balances' phrasing, an anomaly ask, or a 'components add up' phrasing.
_VALIDATION_RE = re.compile(
    r"\b(audit|validat\w+|reconcil\w+|anomal\w+|mis-?extract\w*)\b"
    r"|\bsanity[- ]?check\w*\b"
    r"|\bdoes?\s+(the\s+)?balance\s+sheet\s+balance\b"
    r"|\bbalance\s+sheet[^?.]{0,40}\b(balance|add up|foot|tie out|reconcil\w+|consistent)\b"
    r"|\b(numbers?|figures?|components?|line items?|totals?|assets?)[^?.]{0,40}\b(add up|sum (up )?to|tie out|foot)\b"
    r"|\bsum\b[^?.]{0,30}\b(assets|liabilities|components?|line items?)\b",
    re.I,
)
_OVERVIEW_RE = re.compile(
    r"\bsummar(?:y|i[sz]e)\b|\boverview\b|\bsnapshot\b|at a glance|financial highlights"
    r"|key (?:financial )?(?:metrics|figures|kpis?|highlights|indicators)"
    r"|top \d+ .*(?:kpi|metric|figure)|\bkpis?\b|how is the company (?:doing|performing)",
    re.I,
)
# Driver / decomposition: "which line item drove the largest change in total assets?",
# "what drove the movement in equity?" — needs a driver word AND a change word, OR an
# explicit "largest/biggest … change/contributor".
_DRIVER_RE = re.compile(
    r"(?=.*\b(?:drove|driven|drive[sr]?|contribut\w*|responsible|account(?:ed)?\s+for|caused|led to|behind)\b)"
    r"(?=.*\b(?:change|movement|increase|decrease|growth|swing|rise|fall|delta|shift)\b)"
    r"|\b(?:largest|biggest|main|primary|key)\b[\s\S]{0,40}\b(?:change|movement|driver|contributor)\b",
    re.I,
)
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

    Tries the workbook-specific *matcher* (built from the ontology) FIRST, then falls back to
    the curated _METRIC_KEYWORDS. The fallback matters: the ontology aliases miss common
    phrasings (e.g. "operating profit", "gross profit" resolve to None via the workbook
    matcher, only "operating income" works), so without it those metrics never resolve —
    which is why "operating profit growth vs revenue growth" only saw one side.
    """
    sources = ([matcher] if matcher is not None else []) + [_METRIC_KEYWORDS]
    for source in sources:
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

    # data-validation / balance-sheet audit (deterministic) — high precedence so an audit ask
    # isn't mis-parsed as a lookup/overview. The handler runs identity + footing + anomaly
    # checks in code; the LLM only narrates the findings.
    if _VALIDATION_RE.search(query):
        return QueryFrame(raw_query=query, intent="validation", company=company, year=year)

    # edit-history: changes the USER made via the app (answered from the History log, not
    # financial data). High precedence so "what did I change" isn't mis-read as a lookup.
    if _EDIT_HISTORY_RE.search(query):
        return QueryFrame(raw_query=query, intent="edit_history", company=company, year=year)

    # workbook METADATA / availability (incl. company identity) -> answered from the store's
    # metrics/sheets/years/company. Rules-only intent (the LLM never routes here — see understand).
    if _COMPANY_ID_RE.search(query) or _AVAILABILITY_RE.search(query):
        return QueryFrame(raw_query=query, intent="data_availability", company=company, year=year)

    # ad-hoc formula / unregistered ratio -> agent (compute_expr). Keeps "calculate ROIC = a/(b-c)"
    # off the metric_lookup path, where the composed value would be numeric-guard rejected.
    if _ADHOC_CALC_RE.search(query) or _ADHOC_METRIC_RE.search(query):
        m = _matched_metric(query, metric_matcher)
        return QueryFrame(raw_query=query, intent="agent", company=company, year=year,
                          metrics=([m] if m else []))

    # "<metric> ... as a percentage/share of <other>" -> metric_lookup + ratio (NOT a
    # metric_comparison of two series). Resolve the SUBJECT (the metric before "% of") as
    # primary; the handler computes its share of revenue/total. Skip when it's clearly a
    # comparison or trend ("compare X% of Y over the years").
    _pct = _PCT_OF_RE.search(query)
    if _pct and not _PEER_RE.search(query) and not _TREND_RE.search(query):
        subj = _matched_metric(query[: _pct.start()], metric_matcher)
        if subj:
            return QueryFrame(raw_query=query, intent="metric_lookup", company=company,
                              year=year, metrics=[subj])

    # peer comparison: two companies, or an explicit compare/vs cue
    if len(companies) >= 2 or (_PEER_RE.search(query) and companies):
        formula_id, _ = _matched_formula(query)
        m = _matched_metric(query, metric_matcher)
        return QueryFrame(
            raw_query=query, intent="peer_comparison", company=companies[0],
            companies=companies, year=year, formula=formula_id,
            metrics=([m] if (not formula_id and m) else []),
        )

    # metric comparison (no second company): "operating profit growth vs revenue growth",
    # "current ratio vs quick ratio". Two resolvable metric terms -> metric_comparison.
    if _PEER_RE.search(query) and not companies:
        terms = _comparison_terms(query, metric_matcher)
        if len(terms) >= 2:
            return QueryFrame(raw_query=query, intent="metric_comparison", company=company,
                              year=year, metrics=[t[1] for t in terms])

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

    # overview / summary: "summarize the KPIs", "financial highlights", "key metrics",
    # "top N metrics", "overview/snapshot/at a glance" — a request for the headline figures,
    # NOT a single-metric lookup. Routed to the overview handler which surfaces the
    # workbook's key metrics. (Checked before trend/ratio/metric so it isn't mis-parsed as
    # a lookup for whichever metric word happens to appear.)
    if _OVERVIEW_RE.search(query):
        return QueryFrame(raw_query=query, intent="overview", company=company, year=year)

    # driver / decomposition: "which line item drove the largest change in total assets?"
    # Decomposes a TOTAL's period-over-period change into its component line items and ranks
    # them. Resolve which total from the query (assets / equity+liabilities / revenue).
    if _DRIVER_RE.search(query):
        ql = query.lower()
        if "asset" in ql:
            tgt = "total_assets"
        elif "equity" in ql or "liabilit" in ql:
            tgt = "total_equity_and_liabilities"
        elif "revenue" in ql or "sales" in ql:
            tgt = "revenue"
        else:
            tgt = _matched_metric(query, metric_matcher)
        if tgt:
            return QueryFrame(raw_query=query, intent="driver_analysis", company=company,
                              year=year, metrics=[tgt])

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
    "risk_assessment", "metric_lookup", "overview", "metric_comparison",
    "driver_analysis", "validation", "edit_history", "agent", "unknown",
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
    "CRITICAL follow-up rule: when the current query is ONLY a year or partial year "
    "(e.g. '2025?', '23?', '2024', 'FY25') with no explicit metric, it is a follow-up "
    "asking for the SAME metric(s) as the most recent resolved query in the conversation "
    "history. You MUST copy those metrics into the response — never return an empty "
    "metrics list for a year-only query if the history shows a prior resolved metric. "
    "ASSUMPTION follow-up rule: when the current query is ONLY a number or percentage "
    "(e.g. '1000%?', '25%', 'and 5%') and the most recent prior question was a forecast or "
    "projection, it is the SAME question with the growth/target assumption replaced by this "
    "number — keep the prior intent (forecast_validation or agent) and the prior metric(s) and "
    "year; never return 'unknown'. "
    "Supported intents: peer_comparison, valuation, forecast_validation, earnings_review, "
    "news_impact, dividend_analysis, trend_analysis, ratio_analysis, risk_assessment, "
    "metric_lookup, overview, metric_comparison, driver_analysis, edit_history, agent, unknown. "
    "edit_history is the app's CHANGE LOG of the USER's OWN activity on this workbook in the app "
    "(schema: timestamp, sheet, cell, old, new, saved). It records cell edits, 'manually verified' "
    "toggles, and workbook open/load/upload events. Route here for: what/when/how-many changes the "
    "user made, unsaved changes, changes in a given sheet/session/time-window/date, WHICH SHEET "
    "changed the most, and WHEN the workbook was opened/loaded — e.g. 'what was my last change', "
    "'my last 5 unsaved changes', 'changes I made in the balance sheet', 'changes in this session', "
    "'my changes in the last 5 minutes', 'what did I change on 31 Aug 2025', 'which sheet did I "
    "change most', 'when was this workbook opened'. It reads the app's edit log, NOT the financial "
    "data. Do NOT route here for questions about the data itself, a metric's movement over years "
    "(trend_analysis), a data audit (validation), or a sheet's NAME/CONTENTS — only the user's own "
    "edit activity. "
    "agent is the GENERAL reasoner for open-ended, causal, premise-bearing, or multi-step "
    "questions that don't fit one clean shape. Prefer agent (NOT trend_analysis / "
    "metric_comparison) whenever the question asks WHY a metric changed, why it is "
    "higher/lower/less/more than another value or year, what CAUSED/DROVE/EXPLAINS a change, "
    "or requires combining several steps — e.g. 'why was gross profit lower in 2022 than "
    "2021', 'what explains the margin decline', 'why did ROE fall'. Still set the metric(s) "
    "and year(s) you can infer. The agent will verify the premise against the data and "
    "decompose the change. ALSO route to agent for DATA-VALIDATION / AUDIT questions — 'does the "
    "balance sheet balance', 'do the components sum to the total', 'find anomalies', 'audit the "
    "statements', 'are the numbers consistent' — and for ad-hoc computations the standard ratios "
    "don't cover (e.g. ROIC, cash conversion cycle, per-share math). "
    "overview is for requests to SUMMARIZE the company's key financials / KPIs / highlights "
    "(e.g. 'summarize the top 5 KPIs', 'financial overview') — NOT a single-metric lookup. "
    "metric_comparison is for comparing TWO of the company's own metrics/series — e.g. "
    "'operating profit growth vs revenue growth', 'current ratio vs quick ratio' — when no "
    "second company is named (two companies = peer_comparison). A question asking for one metric "
    "AS A PERCENTAGE / SHARE / FRACTION of another (e.g. 'what was gross profit in 2021, and what "
    "% of revenue was it', 'cost of sales as a share of revenue in 2024') is metric_lookup (a "
    "single value plus its ratio) — NOT metric_comparison; put the SUBJECT metric first in "
    "metrics (e.g. ['gross_profit','revenue']). "
    "driver_analysis is for 'what/which line item drove the largest change in <total>' — "
    "decomposing a total (assets, equity+liabilities, revenue) into the component that moved most. "
    "validation is the DATA-AUDIT intent — 'does the balance sheet balance', 'do the components "
    "sum to the total', 'audit/validate the statements', 'find anomalies', 'are the numbers "
    "consistent'. It runs deterministic identity/footing/anomaly checks. "
    "Use agent for FORWARD PROJECTIONS — 'project/estimate revenue for 2026', 'what will X be next "
    "year' (the agent projects a scenario range). Reserve forecast_validation for judging a STATED "
    "target ('is a 10% growth forecast reasonable', 'is guidance on track'). "
    "INTENT GUIDANCE: risk_assessment is the QUALITATIVE-INSIGHTS handler — route ANY "
    "narrative/qualitative question that can be answered from the company's management "
    "commentary or report insights here, NOT just questions containing the word 'risk'. "
    "This includes questions about what management says or expects, demand, market share, "
    "industry / business / economic conditions, operational challenges, strategy, "
    "competitive position, outlook, guidance, strengths/weaknesses, and risks. "
    "metric_lookup / ratio_analysis / trend_analysis are for specific NUMERIC figures or "
    "ratios; risk_assessment is for everything qualitative about this company. "
    "Use 'unknown' ONLY when the query is not about this company's financials or "
    "qualitative disclosures at all (e.g. a greeting or an unrelated topic) — whenever the "
    "question is about the company's performance, position, or commentary, pick a supported "
    "intent (default to risk_assessment for qualitative questions) rather than 'unknown'. "
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

    # Step 3: single GPT call — expand + validate in one shot.
    # data_availability is a RULES-ONLY intent: the LLM validator isn't told it exists, so we
    # must NOT send it through (the LLM would re-classify it into a value intent). Keeping the
    # rules frame here is also what guarantees the LLM can never pull OTHER queries into
    # data_availability — the regexes in build_frame are the only door.
    if frame.intent == "data_availability":
        result = frame
        _log.info("fie understand: data_availability (rules-only) — skipping LLM validation",
                  extra={"component": "Understand"})
    elif llm is not None:
        validated = validate_frame_llm(
            query, frame, llm, available_metrics, available_years, history
        )
        result = validated if validated is not None else frame
        if validated is None:
            _log.info("fie LLM validation returned None — using rules frame (intent=%r)",
                      frame.intent, extra={"component": "Understand"})
    else:
        _log.debug("fie understand: no LLM configured, using rules frame only",
                   extra={"component": "Understand"})
        result = frame

    # Deterministic backstop: a bare follow-up that is just a year ('2025?') or an assumption
    # ('1000%?') after a forecast/projection/calc re-runs that question with the year/assumption
    # swapped (works even if the LLM left it unresolved).
    result = _resolve_followup(result, query, history, available_years)
    _log.info(
        "fie understand final: intent=%r year=%s metrics=%s formula=%r source=%r",
        result.intent, result.year, result.metrics, result.formula, result.source,
        extra={"component": "Understand"},
    )
    return result
