"""Core data types for the Financial Intelligence Engine (FIE).

These are the frozen contract every later layer depends on. All models are
pydantic v2 and JSON/dict-serializable via ``model_dump()`` / ``model_validate()``.

See docs/fie_phase0_foundation.md §4.
"""

from __future__ import annotations

import re as _re
from typing import Literal, Optional, TypedDict

from pydantic import BaseModel, Field, model_validator

# --- documented shapes for the heterogeneous provenance/coverage dicts ------
# The fields below stay plain `dict`/`list[dict]` at RUNTIME (pydantic couples a typed
# field to both validation AND serialization, and these dicts are intentionally open
# and heterogeneous — enforcing a schema either rejects None/coerces ints on input or
# drops source-specific keys on model_dump). These TypedDicts (`total=False`) document
# the known keys in ONE place and give static checkers / IDEs a typo-checkable schema
# when a local or helper is annotated with them. They are NOT used as pydantic field
# types — see the field comments that reference them.

class Locator(TypedDict, total=False):
    # workbook / insight provenance
    report_file: Optional[str]
    page: Optional[int]
    table_id: Optional[str]
    sheet: Optional[str]
    cell: Optional[str]
    year: Optional[int]
    report_year: Optional[int]
    insight_id: Optional[str]
    basis: Optional[str]
    # external provenance (market data / news / datasets)
    source: Optional[str]
    provider: Optional[str]
    author: Optional[str]
    link: Optional[str]
    url: Optional[str]
    field: Optional[str]
    metric: Optional[str]
    symbol: Optional[str]
    company: Optional[str]
    retrieved_at: Optional[str]


class Coverage(TypedDict, total=False):
    degraded: bool
    partial_coverage: bool
    dropped_insights: int
    superseded_insights: int
    withheld: int
    admission: dict
    dropped_claims: int
    qualitative: dict
    insufficient_evidence: bool


class ConfidenceComponent(TypedDict, total=False):
    name: str
    value: float
    rationale: str


class ConflictValue(TypedDict, total=False):
    source: Optional[str]
    value: Optional[float]
    unit: Optional[str]
    canonical: Optional[float]
    authority: Optional[str]
    insight_id: Optional[str]
    area: Optional[str]
    year: Optional[int]
    takeaway: Optional[str]


# --- enums (closed sets) ---------------------------------------------------

ValidationStatus = Literal["CLEAN", "MISMATCH", "WITHHELD", "NO_FACE_TRUTH"]
PeriodType = Literal["historical", "forecasted"]
Statement = Literal["pl", "bs", "cf", "equity", "other"]
Level = Literal["headline", "detail"]
ProvenanceBasis = Literal["direct", "via_detail", "workbook", "none"]
CitationKind = Literal["financial", "insight", "external", "forecast"]
EvidenceKind = Literal["statement", "detail", "insight", "external", "calc"]


class FactRef(BaseModel):
    """Immutable identity of a single financial value plus its provenance.

    A ``FactRef`` travels with every number from the moment it is read so that
    citations and confidence can be read off it rather than reconstructed.
    """

    company: str
    metric: Optional[str]  # canonical id; None when the label is unmapped
    label: str  # raw workbook label (kept verbatim, even with encoding artifacts)
    year: int
    period_type: PeriodType = "historical"
    value: Optional[float]  # None when absent / withheld
    unit: str = "Rupees in thousand"
    statement: Statement
    level: Level
    sheet: str
    cell: str

    # provenance (resolved via the Source Ledger; see §6)
    source_ref: Optional[str] = None  # "<report_file>:p<page>:<table_id>"
    report_year: Optional[int] = None
    provenance_basis: ProvenanceBasis = "none"

    # dev-only annotation; runtime ignores this (architecture §0.3)
    validation_status: ValidationStatus = "CLEAN"


class Citation(BaseModel):
    """A resolvable, human-displayable pointer to a source for a claim."""

    ref_id: str  # inline handle, e.g. "C7"
    kind: CitationKind
    display: str  # e.g. "MTL Annual Report 2024-25, p108"
    locator: dict = Field(default_factory=dict)  # shape: Locator (see TypedDicts above)
    confidence: Optional[float] = None  # Source Ledger confidence, if present
    retrieved_at: Optional[str] = None  # external only


class EvidenceItem(BaseModel):
    """A normalized unit of evidence: internal cell, insight, external datum, or calc."""

    claim: str
    value: Optional[float] = None
    unit: Optional[str] = None
    kind: EvidenceKind
    fact_refs: list[FactRef] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    reliability: float = 1.0
    freshness: Optional[str] = None  # ISO date
    as_of: Optional[str] = None
    role: Optional[str] = None  # admission role (baseline|supporting|event_fact|…); see admission.py


class NewsArticle(BaseModel):
    """A single news item from an external provider — stores both the CONTENT
    (title/description/body) and the SOURCE (publisher + url + which provider
    served it + publish time). This is the typed unit the news layer persists;
    the adapter converts each one to an external, cited EvidenceItem for the
    engine (the full article is preserved in the citation locator)."""

    # content
    title: str
    description: Optional[str] = None      # snippet / summary
    content: Optional[str] = None          # body text when the provider returns it
    # source / provenance
    url: Optional[str] = None
    source: Optional[str] = None           # publisher (e.g. "Reuters", "WSJ")
    author: Optional[str] = None           # byline when the provider supplies it
    provider: str = ""                     # which API served it (marketaux/finnhub/…)
    published_at: Optional[str] = None     # ISO timestamp
    language: Optional[str] = None
    # finance extras when available
    symbols: list[str] = Field(default_factory=list)   # tagged tickers/entities
    sentiment: Optional[float] = None


# --- query understanding / planning (L1/L2) --------------------------------

ConfidenceBand = Literal["High", "Medium", "Low"]


class QueryFrame(BaseModel):
    """Structured intent + entities extracted from the user query (L1).

    Phase 1 fills this with rules only; Phase 3 adds an LLM builder behind the
    same schema.
    """

    raw_query: str
    intent: str
    company: Optional[str] = None
    companies: list[str] = Field(default_factory=list)  # peer_comparison targets
    year: Optional[int] = None
    years: list[int] = Field(default_factory=list)  # explicit multi-year range (trend)
    window: Optional[int] = None  # "last N years" — resolved against the store in the engine
    aggregation: Optional[str] = None  # trend operator: "average" | "cagr" | None
    formula: Optional[str] = None  # e.g. "current_ratio"
    metrics: list[str] = Field(default_factory=list)  # required canonical metrics
    level: Level = "headline"
    period_type: PeriodType = "historical"
    # restatement-aware temporal resolution (architecture §2.4): default to the
    # newest report's view of a (possibly restated) prior year.
    report_year_preference: Literal["latest", "as_reported"] = "latest"
    source: Literal["rules", "llm"] = "rules"  # which builder produced this frame


class SourceRequirement(BaseModel):
    kind: Literal["internal", "external"] = "internal"
    metric: str
    year: int
    level: Level = "headline"
    period_type: PeriodType = "historical"


class SourcePlan(BaseModel):
    """A (Phase 1: linear) set of retrieval requirements + an optional calc (L2)."""

    requirements: list[SourceRequirement] = Field(default_factory=list)
    formula: Optional[str] = None
    external_sources: list[str] = Field(default_factory=list)  # which external adapters to consult
    notes: list[str] = Field(default_factory=list)


# --- calculation / confidence / response (L4/L7/L8) ------------------------


class CalcResult(BaseModel):
    formula_id: str
    value: Optional[float]
    unit: str = "ratio"
    inputs: list[FactRef] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    confidence: ConfidenceBand = "Medium"
    expression: Optional[str] = None  # human-readable, e.g. "current_assets / current_liabilities"
    note: Optional[str] = None


class ConfidenceReport(BaseModel):
    band: ConfidenceBand
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    caps_applied: list[str] = Field(default_factory=list)
    # min-weakest-link composition: the final score is min(component values); the
    # binding (lowest) component is named so the band is explainable.
    limited_by: Optional[str] = None
    components: list[dict] = Field(default_factory=list)  # shape: ConfidenceComponent


ConflictType = Literal[
    "insight_vs_insight", "restatement", "forecast_vs_actual",
    "internal_vs_external", "cross_api", "insight_vs_disclosure",
]


class Conflict(BaseModel):
    """A runtime conflict (insights / restatement / external) — NOT computed-vs-stated.

    The financial core is trusted, so there is no computed-vs-stated conflict at
    runtime (architecture §0.3 / §8).
    """

    type: ConflictType
    topic: str  # metric, area, or claim subject
    year: Optional[int] = None
    values: list[dict] = Field(default_factory=list)  # shape: ConflictValue (competing items)
    resolution: Optional[str] = None  # winner + rationale, or None if exposed
    resolved: bool = False


class ReasoningGraph(BaseModel):
    """Explicit premises → inferences → conclusion (L5). Every premise references
    cited evidence; the LLM narrates *over* this, it does not invent it."""

    premises: list[str] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    conclusion: str = ""


class Response(BaseModel):
    """The structured, cited answer (L8)."""

    direct_answer: str
    key_findings: list[str] = Field(default_factory=list)
    supporting_analysis: str = ""
    calculations: list[CalcResult] = Field(default_factory=list)
    evidence_used: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    conflicts: list["Conflict"] = Field(default_factory=list)
    withheld: list[str] = Field(default_factory=list)
    confidence: Optional[ConfidenceReport] = None
    prose_source: Literal["deterministic", "llm"] = "deterministic"
    coverage: dict = Field(default_factory=dict)  # shape: Coverage (see TypedDicts above)

    @model_validator(mode="after")
    def _findings_must_cite(self) -> "Response":
        """Boundary invariant ("no citation, no claim"): every shipped key finding
        must carry a resolvable inline citation handle [Cn]. The renderer enforces
        this upstream (dropping uncitable claims); this is the structural backstop."""
        for f in self.key_findings:
            if not _re.search(r"\[C\d+\]", f):
                raise ValueError(f"key finding lacks a citation handle: {f!r}")
        return self
