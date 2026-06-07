"""Source selection / planner (L2) — Phase 1 fixed internal-only plan.

Maps a QueryFrame to a linear SourcePlan of internal lookups. No external
sources, no DAG yet. See docs/fie_implementation_plan.md §Phase 1 (1.3).
"""

from __future__ import annotations

from .apis.registry import shortlist as _shortlist
from .models import QueryFrame, SourcePlan, SourceRequirement

# the external adapter catalog the planner may select from (LLM augmentation is
# validated against this set). Every value used in _INTENT_SOURCES must appear here.
# NOTE: only sources with a real fetcher belong here — a catalog entry with no adapter
# (formerly "macro") would be planned, logged, then silently dropped at retrieval.
SOURCE_CATALOG = {"psx", "news", "forecast", "psx_announcements", "secp",
                  "company_payouts"}

# rule-based intent -> external sources.
# NOTE (deliberate): only intents that benefit from a *named* external feed are mapped.
# ratio_analysis / metric_lookup / trend_analysis / peer_comparison are intentionally
# internal-only here — they are still corroborated against same-period external actuals
# by the engine's analysis_reports path (fie._corroborate), which is orthogonal to this
# planner source list.
_INTENT_SOURCES: dict[str, list[str]] = {
    "valuation": ["psx"],
    # news_impact / earnings_review / risk_assessment fetch PSX disclosures via the
    # query-driven registry path (registry_apis), so only the multi-provider "news" token
    # lives here; the PSX side is the registry floor + shortlist (see _registry_apis).
    "news_impact": ["news"],
    "earnings_review": ["news"],
    # forecast_validation: external analyst forecast (repo) + news; PSX disclosures /
    # analyst reports come via the query-driven registry path (registry_apis), giving
    # management guidance / capex signals to judge whether a target is realistic.
    "forecast_validation": ["forecast", "news"],
    "dividend_analysis": ["company_payouts"],
    "risk_assessment": ["news"],
}
# Query-driven registry selection (apis.registry catalog of 17 APIs). For intents that
# consult external data via the generic RegistryFetcher path (`_fetch_external`), we pick a
# RELEVANT SUBSET per query with shortlist(), unioned with a per-intent FLOOR so a
# rule-critical API is never dropped by a weak token match. Numeric intents (valuation /
# dividend_analysis / forecast_validation) still use their dedicated bespoke adapters and
# are intentionally NOT registry-driven here.
_REGISTRY_INTENTS = {"news_impact", "earnings_review", "risk_assessment", "forecast_validation"}
_INTENT_REGISTRY_FLOOR: dict[str, list[str]] = {
    "news_impact": ["company_announcements"],
    "earnings_review": ["company_announcements"],
    "risk_assessment": ["company_announcements", "secp_notices"],
    "forecast_validation": ["company_announcements", "analysis_reports"],
}
_REGISTRY_TOP_K = 5  # cap the query-driven subset so we never fan out to all 17


def _registry_apis(frame: QueryFrame) -> list[str]:
    """Relevant subset of the registry catalog for this query = intent floor + shortlist()."""
    if frame.intent not in _REGISTRY_INTENTS:
        return []
    out = list(_INTENT_REGISTRY_FLOOR.get(frame.intent, []))   # floor first (guaranteed)
    for api, _score in _shortlist(frame.raw_query, intent=frame.intent, top_k=_REGISTRY_TOP_K):
        if api.name not in out:
            out.append(api.name)
    return out

def plan(frame: QueryFrame, llm=None) -> SourcePlan:
    notes: list[str] = []
    requirements: list[SourceRequirement] = []

    if frame.year is None and frame.intent in {"ratio_analysis", "metric_lookup"}:
        notes.append("no year resolved from query")

    # metric_lookup needs explicit internal fetches; calc-driven intents self-fetch
    if frame.intent == "metric_lookup" and frame.metrics and frame.year is not None:
        for metric in frame.metrics:
            requirements.append(SourceRequirement(
                kind="internal", metric=metric, year=frame.year,
                level=frame.level, period_type=frame.period_type,
            ))

    # external source selection: rule-chosen tokens + the deterministic, query-driven
    # registry shortlist (below). The old LLM source-augmentation call was removed — it is
    # now redundant: shortlist()+intent-floor selects the query-relevant subset for free,
    # saving one GPT request per external-data query with no quality loss.
    external = list(_INTENT_SOURCES.get(frame.intent, []))

    registry_apis = _registry_apis(frame)
    if registry_apis:
        notes.append(f"registry subset: {registry_apis}")

    return SourcePlan(requirements=requirements, formula=frame.formula,
                      external_sources=external, registry_apis=registry_apis, notes=notes)
