"""Source selection / planner (L2) — Phase 1 fixed internal-only plan.

Maps a QueryFrame to a linear SourcePlan of internal lookups. No external
sources, no DAG yet. See docs/fie_implementation_plan.md §Phase 1 (1.3).
"""

from __future__ import annotations

from .models import QueryFrame, SourcePlan, SourceRequirement

# the external adapter catalog the planner may select from
SOURCE_CATALOG = {"psx", "news", "forecast", "macro", "psx_announcements", "secp"}

# rule-based intent -> external sources
_INTENT_SOURCES: dict[str, list[str]] = {
    "valuation": ["psx"],
    "news_impact": ["news", "psx_announcements"],
    "earnings_review": ["news", "psx_announcements"],
    "forecast_validation": ["forecast"],
    "dividend_analysis": ["company_payouts"],
}
# intents where the LLM may *augment* the rule-chosen sources
_EXTERNAL_INTENTS = set(_INTENT_SOURCES)

_LLM_SYS = (
    "Given a financial query and intent, list which external data sources are needed. "
    f"Choose only from: {sorted(SOURCE_CATALOG)}. Respond JSON: {{\"sources\": [..]}}."
)
_LLM_SCHEMA = {"type": "object",
               "properties": {"sources": {"type": "array", "items": {"type": "string"}}},
               "required": ["sources"]}


def _llm_sources(frame: QueryFrame, llm) -> list[str]:
    """LLM-assisted fallback/augmentation for source selection (validated to catalog)."""
    if llm is None or frame.intent not in _EXTERNAL_INTENTS:
        return []
    data = llm.complete_json(_LLM_SYS, f"intent={frame.intent}; query={frame.raw_query}",
                             _LLM_SCHEMA)
    if not isinstance(data, dict):
        return []
    return [s for s in data.get("sources", []) if s in SOURCE_CATALOG]


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

    # external source selection: rules first, LLM-assisted augmentation second
    external = list(_INTENT_SOURCES.get(frame.intent, []))
    for s in _llm_sources(frame, llm):
        if s not in external:
            external.append(s)
            notes.append(f"LLM-added source: {s}")

    return SourcePlan(requirements=requirements, formula=frame.formula,
                      external_sources=external, notes=notes)
