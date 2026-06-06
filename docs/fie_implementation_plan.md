# Financial Intelligence Engine — Implementation Plan

**Companion to:** [`financial_intelligence_engine.md`](financial_intelligence_engine.md) (architecture)
**Stack:** Python 3 · pydantic v2 · pandas/numpy · FastAPI · pytest · openai
**Strategy:** Foundation → walking skeleton (one vertical slice) → deepen deterministic
core → add LLM layers → external orchestration → hardening. **Not** pure bottom-up
layer-by-layer — we get one query answering end-to-end early, then deepen.

**Proposed package layout** (under `backend/app/engines/fie/`):

```
fie/
  models.py          # FactRef, EvidenceItem, Citation, QueryFrame, ... (pydantic)
  store.py           # FinancialFactStore  (L0 ingestion: workbook → DataFrame/JSON)
  understanding.py   # QueryFrame builder   (L1, rules + LLM)
  planner.py         # SourcePlan builder   (L2)
  retrieval.py       # internal lookups     (L3a)
  apis/              # ApiSpec + clients     (L3b)
  calc/
    engine.py        # formula evaluator    (L4)
    registry.py      # FormulaSpec registry  (L4 / deliverable #7)
  synthesis.py       # evidence align/reason (L5)
  conflicts.py       # detection + resolution (L6)
  confidence.py      # scoring rubric        (L7)
  citations.py       # binding/resolution    (L8a)
  response.py        # render structured ans (L8b)
  trace.py           # TraceRecord store     (L9)
  fie.py             # orchestrator (wires L0..L9)
tests/fie/
  fixtures/golden_millat.py   # audit-derived golden values
  test_*.py
```

---

## Phase 0 — Foundation (data model + ingestion)

*The only genuinely bottom-up part: everything depends on it.*

### Tasks
- **0.1** Define core pydantic models in `models.py` (signatures below).
- **0.2** Implement `FinancialFactStore.from_workbook(path)` — load **findata + ledgers → pandas DataFrames** (long format), **insights → list[dict] (JSON)**. (See §0.2 / §0.3 of the architecture doc.)
- **0.3** Implement `store.lookup(metric, year, company)` → `FactRef`, joining the `Source Ledger` (provenance) and `Validation Ledger` (status) onto each value.
- **0.4** Write the golden-fixture module (Phase-G below) — **before** any calc logic.
- **0.5** Smoke test: load `millat_filled_fixed.xlsx` and `lucky_filled_fixed.xlsx`; assert sheet set, row counts, and that every headline `FactRef` resolves a `source_ref` or is flagged `NO_FACE_TRUTH`.

### Interface signatures
```python
# models.py
from pydantic import BaseModel
from typing import Literal, Optional

ValidationStatus = Literal["CLEAN", "MISMATCH", "WITHHELD", "NO_FACE_TRUTH"]

class FactRef(BaseModel):
    company: str
    metric: str                      # canonical metric id (via ontology)
    year: int                        # fiscal year
    value: Optional[float]           # None when WITHHELD
    unit: str = "Rupees in thousand"
    sheet: str
    cell: str
    source_ref: Optional[str]        # "<report_file>:p<page>:<table_id>"
    report_year: Optional[int]       # which report this value came from
    validation_status: ValidationStatus = "CLEAN"
    face_truth: Optional[float] = None

class EvidenceItem(BaseModel):
    claim: str
    value: Optional[float] = None
    unit: Optional[str] = None
    kind: Literal["statement", "detail", "insight", "external", "calc"]
    fact_refs: list[FactRef] = []
    reliability: float = 1.0         # 0..1
    freshness: Optional[str] = None  # ISO date
    as_of: Optional[str] = None

class Citation(BaseModel):
    ref_id: str                      # inline handle e.g. "C7"
    kind: str
    display: str                     # "Annual Report 2025, p108, Income Statement"
    locator: dict                    # {report_file, page, table_id, sheet, cell}
    retrieved_at: Optional[str] = None
    validation_status: ValidationStatus = "CLEAN"

# store.py
class FinancialFactStore:
    @classmethod
    def from_workbook(cls, path: str) -> "FinancialFactStore": ...
    def lookup(self, metric: str, year: int, company: str | None = None,
               report_year_pref: Literal["latest", "as_reported"] = "latest") -> FactRef: ...
    def detail(self, metric: str, year: int) -> "pandas.DataFrame": ...      # PL*/BS* line items
    def insights(self, *, area: str | None = None, year: int | None = None,
                 min_confidence: float = 0.0) -> list[dict]: ...
    def validation(self, metric: str, year: int) -> ValidationStatus: ...
    @property
    def manifest(self) -> dict: ...   # production_ready, fully_reconciled, ...
```

**Exit criteria:** both workbooks load; `lookup("revenue", 2024)` returns a `FactRef`
with a resolvable `source_ref`; golden fixtures importable.

---

## Phase 1 — Walking skeleton (one vertical slice)

*Thinnest path through every layer for ONE intent: `ratio_analysis`, internal-only,
no LLM, no external APIs. Proves the `FactRef`-carries-provenance contract end to end.*

### Tasks
- **1.1** `fie.py` orchestrator stub wiring L0→L8 for a single hardcoded intent.
- **1.2** Trivial `understanding.build_frame()` — regex/keyword only (no LLM yet): detect metric + year + company.
- **1.3** Trivial `planner.plan()` — returns a fixed internal-only `SourcePlan`.
- **1.4** `retrieval.fetch()` — calls `store.lookup` for required metrics.
- **1.5** Minimal `calc` path — one formula (`current_ratio`) hardwired.
- **1.6** `citations.bind()` — resolve each `FactRef.source_ref`; withhold unciteable.
- **1.7** `response.render()` — emit the 7-section structure (plain dict/markdown).
- **1.8** Golden test: `"current ratio for MTL 2024"` returns a number **and** a citation to a `Source Ledger` row.

### Interface signatures
```python
# fie.py
class FinancialIntelligenceEngine:
    def __init__(self, store: FinancialFactStore, *, llm=None, apis=None): ...
    def answer(self, query: str) -> "Response": ...   # the single public entrypoint

# response.py
class Response(BaseModel):
    direct_answer: str
    key_findings: list[str]
    supporting_analysis: str
    calculations: list["CalcResult"]
    evidence_used: list[str]
    citations: list[Citation]
    confidence: Optional["ConfidenceReport"] = None
```

**Exit criteria:** one real query → correct, cited answer through all layers. Lock the
layer interfaces here; later phases deepen implementations, not signatures.

---

## Phase 2 — Deepen the deterministic core (highest trust value)

*All rule-based. **Runtime trusts the workbook's financial figures (§0.3 of the
architecture doc)** — the engine derives metrics from them and does not re-litigate
their correctness at answer time. The recompute / face-truth / `Validation Ledger`
machinery is therefore built as a **separate development-time validation harness
(Phase G)**, not part of the answer path.*

### Tasks (runtime engine)
- **2.1** **Formula registry** (`calc/registry.py`): declarative `FormulaSpec`s for growth, profitability, liquidity, leverage, cash flow, valuation, forecast-validation (architecture §5.3).
- **2.2** **Calc engine** (`calc/engine.py`): read authoritative workbook values; derive metrics the workbook lacks; enforce `domain_guards` (divide-by-zero, sign conventions — `cost_of_sales` is stored negative); attach all input citations; result confidence = min(inputs). Prefer a stored value over recomputation.
- **2.3** **Insight selection & ranking** (`insights.py`): relevance filter (entity/topic + temporal + intent affinity); **insight-vs-insight conflict resolution by year → confidence**, with a configurable blended-score mode (`α`, default 0.7); retain superseded insights as caveats; log dropped/unused counts. (Architecture §3.4.)
- **2.4** **Conflict detection** (`conflicts.py`): **runtime types only** — insight-vs-insight, restatement (same `(metric, year)`, different `report_year` *within the workbook*), internal-vs-external, cross-API, insight-vs-disclosure. **No** computed-vs-stated / `Validation Ledger` ingestion at runtime.
- **2.5** **Conflict resolution**: precedence ladder (workbook financial figure authoritative → newest report year → insight recency-then-confidence → reliability → freshness); resolve+explain or expose-both.
- **2.6** **Confidence scoring** (`confidence.py`): weighted rubric + caps (architecture §9.2) — insight-strength / freshness / coverage / unresolved-conflict caps; **no financial-mismatch or reconciliation cap**.
- **2.7** **Citation system** (`citations.py`): full resolve/withhold + `Citation.display` formatting against the in-workbook `Source Ledger`.

### Tasks (development-time validation harness — separate, not in answer path)
- **2.8** **Extraction validator** (`devtools/validate_extraction.py`): recompute headline metrics from `PL*`/`BS*` detail; cross-check against PDF face truths + the `Validation Ledger`/manifest; emit a pass/fail report. **This is where the audit golden set (Phase G) runs.** It validates that the "financial data is correct" assumption holds for a given workbook before the FIE is pointed at it — it never executes during a user query.
- **2.9** Run the **full golden suite** (Phase-G) via the validator; all green before Phase 3.

### Interface signatures
```python
# calc/registry.py
class FormulaSpec(BaseModel):
    id: str
    category: Literal["growth","profitability","liquidity","leverage",
                      "cashflow","valuation","forecast"]
    expression: str                    # "gross_profit / revenue"
    inputs: list[dict]                 # [{metric, required}]
    domain_guards: list[str] = []      # ["revenue != 0"]
    output_unit: Literal["ratio","percent","currency"]
    rounding: int = 4
    version: str = "1.0"

class FormulaRegistry:
    def register(self, spec: FormulaSpec) -> None: ...
    def get(self, formula_id: str) -> FormulaSpec: ...

# calc/engine.py
class CalcResult(BaseModel):
    formula_id: str
    value: Optional[float]
    inputs: list[FactRef]
    citations: list[Citation]
    confidence: Literal["High","Medium","Low"]
    note: Optional[str] = None         # e.g. "derived; gross_profit not stored"

class CalcEngine:
    def evaluate(self, formula_id: str, year: int,
                 company: str | None = None) -> CalcResult: ...

# insights.py  (runtime insight selection + ranking, architecture §3.4)
class InsightSelector:
    def select(self, frame: "QueryFrame", insights: list[dict],
               *, min_relevance: float = 0.5) -> list[dict]: ...
    def resolve_conflicts(self, selected: list[dict], *,
                          mode: Literal["year_then_confidence","blended"]
                              = "year_then_confidence",
                          alpha: float = 0.7) -> dict:
        # year_then_confidence: argmax over (Year, then Confidence)
        # blended:  score = alpha*recency_norm(Year) + (1-alpha)*Confidence
        # returns {winner, superseded: [...], rationale}
        ...

# conflicts.py  (runtime types only — no computed-vs-stated; §0.3)
class Conflict(BaseModel):
    type: Literal["insight_vs_insight","restatement","forecast_vs_actual",
                  "internal_vs_external","cross_api","insight_vs_disclosure"]
    metric: str; year: int
    values: list[FactRef | EvidenceItem | dict]   # dict = insight record
    resolution: Optional[str] = None   # winner + rationale, or None if exposed
    resolved: bool = False

class ConflictResolver:
    def detect(self, evidence: list[EvidenceItem]) -> list[Conflict]: ...
    def resolve(self, conflict: Conflict) -> Conflict: ...

# confidence.py
class ConfidenceReport(BaseModel):
    band: Literal["High","Medium","Low"]
    score: float
    reasons: list[str]
    caps_applied: list[str] = []

class ConfidenceScorer:
    # no `manifest` arg: reconciliation flags are dev-time signals, not runtime inputs (§0.3)
    def score(self, evidence: list[EvidenceItem], calcs: list[CalcResult],
              conflicts: list[Conflict], selected_insights: list[dict]) -> ConfidenceReport: ...
```

**Exit criteria — runtime engine:** insight selection resolves a year/confidence
conflict correctly; derived metrics carry input citations; runtime conflict types
detected and resolved/exposed per §8.

**Exit criteria — validation harness (2.8/2.9):** every golden fixture passes — audited
face-truth values reproduced from detail, and the known-bad v5 values correctly flagged
as `MISMATCH` by the validator. (This gates *whether a workbook is fit to feed the FIE*,
not any single answer.)

---

## Phase 3 — LLM layers (behind typed boundaries)

*The swappable part. Add only once the numeric core is verified.*

### Tasks
- **3.1** `understanding.py`: LLM-backed `QueryFrame` builder constrained to a tool-call schema (rules remain the high-precision first pass); restatement-aware temporal resolution.
- **3.2** `synthesis.py`: build the `ReasoningGraph`; LLM narrates **only over** validated `EvidenceItem`/`CalcResult` (no raw numbers introduced).
- **3.3** `response.py`: LLM prose for `direct_answer`/`supporting_analysis`, with a **render-time guard** — every emitted figure must match an in-scope `FactRef` (reuse `citations.bind`).
- **3.4** Audience modes (`analyst` | `investor`).
- **3.5** Hallucination test: assert no numeric token in the prose lacks a backing `FactRef`.

### Interface signatures
```python
# understanding.py
class QueryFrame(BaseModel):
    intent: str
    entities: dict                     # {company, metric, fiscal_year, ...}
    required_sources: list[str]
    temporal: dict                     # {fiscal_year, report_year_preference}
    comparison: Optional[str] = None

class QueryUnderstanding:
    def build_frame(self, query: str) -> QueryFrame: ...

# synthesis.py
class ReasoningGraph(BaseModel):
    premises: list[EvidenceItem]
    inferences: list[str]
    conclusion: str

class Synthesizer:
    def synthesize(self, evidence: list[EvidenceItem],
                   calcs: list[CalcResult]) -> ReasoningGraph: ...
```

**Exit criteria:** Phase-1 query still passes via the LLM path; hallucination guard green.

### Implementation note (built)

- **LLM is optional and behind `LLMClient`** ([llm.py](../backend/app/engines/fie/llm.py)):
  `NullLLM` (default → fully deterministic path, 46 prior tests unchanged), `OpenAILLM`
  (lazy, never exercised in tests). Engine accepts `llm=`.
- **Understanding** ([understanding.py](../backend/app/engines/fie/understanding.py)):
  rules remain the high-precision first pass; `understand()` calls the LLM **only** when
  rules return `unknown`. Unknown/unsupported intents and unknown formula ids from the
  model are dropped (never trusted). `QueryFrame.source` records `rules|llm`.
- **Numeric guard** ([safety.py](../backend/app/engines/fie/safety.py)) — the enforcement
  of "no citation, no claim" for prose: every numeric token in LLM text must match an
  in-scope figure (FactRef/CalcResult value within 1%), a cited page/report-year, an
  in-scope fiscal year, or a small structural count. Citation handles (`[C12]`) and unit
  phrases (`Rs '000`) are stripped before checking. **On any violation the LLM prose is
  rejected and the deterministic renderer is used** (`Response.prose_source` = `llm` |
  `deterministic`). The guard is deliberately strict — even benchmark phrasing like
  "above 1.0x" triggers fallback because `1.0` is not in evidence. Trade-off: fewer LLM
  sentences survive, but a fabricated figure can never reach the user.
- **Audience modes** (`analyst` | `investor`) change verbosity only, never figures.

---

## Phase 4 — External orchestration + hard intents

### Tasks
- **4.1** `apis/` base: `ApiSpec` + resilient client (timeout, backoff, circuit-breaker, cached-last-good with `retrieved_at`), normalizer → `EvidenceItem`.
- **4.2** PSX adapter (profiles, results, announcements, market stats); **unit/scale reconciliation** to "Rupees in thousand".
- **4.3** News + Macro adapters.
- **4.4** Forecast repository adapter (versioned forecasts).
- **4.5** Enable external-dependent intents: `forecast_validation`, `news_impact`, `peer_comparison`, `valuation`, `earnings_review`.
- **4.6** Graceful degradation test: API down → answer proceeds on internal data with confidence capped to Medium.

### Interface signatures
```python
# apis/base.py
class ApiSpec(BaseModel):
    id: str; endpoint: str; method: str = "GET"
    parameters: dict; reliability_rating: float
    refresh_frequency: str
    failure_mode: Literal["cache","degrade","omit"]

class ApiClient:
    def fetch(self, spec: ApiSpec, **params) -> list[EvidenceItem]: ...   # already normalized
```

**Exit criteria:** HUBC `forecast_validation` and an MTL-vs-Lucky `peer_comparison`
return cited, confidence-scored answers; degradation path verified.

### Implementation note (built)

- **Resilient client** ([apis/base.py](../backend/app/engines/fie/apis/base.py)):
  `ApiClient` over an injectable `Transport` (so fully testable offline) — timeout,
  retry-with-backoff (injectable sleep/clock), per-spec **circuit breaker**, and
  **last-good cache** keyed by (spec, params). `failure_mode` ∈ `cache|degrade|omit`.
  Normalizers emit cited `EvidenceItem`s (kind=`external`) with `retrieved_at`/reliability.
- **Adapters**: [psx.py](../backend/app/engines/fie/apis/psx.py) (price/EPS, unit label
  `PKR/share`), [news.py](../backend/app/engines/fie/apis/news.py) (date-aware headlines),
  [forecast.py](../backend/app/engines/fie/apis/forecast.py) (`ForecastRepo`: injected
  overrides → in-workbook `forecasted` columns). `Macro` left as a future adapter (same
  pattern). Bundled in `ExternalSources` (+ a `peers` store registry for multi-workbook).
- **Hard intents** wired in the orchestrator: `peer_comparison` (multi-workbook, internal —
  e.g. current ratio MTL 1.24x vs Lucky 1.26x), `valuation` (P/E from PSX price/EPS),
  `forecast_validation` (forecast vs latest internal actual — e.g. 2026 rev forecast 15.1%
  above FY2025 actual), `news_impact` / `earnings_review` (news evidence).
- **Graceful degradation** ([confidence.py](../backend/app/engines/fie/confidence.py)
  `degraded`/`partial_coverage` caps): a missing/down external source caps confidence at
  **Medium** while the answer proceeds on internal data (forecast_validation still reports
  the latest actual). A *purely* external metric with no internal fallback (valuation, PSX
  down) returns **Low** with "internal financials remain available" — honest, not capped up.
- Tested entirely offline with fake transports (retries, circuit breaker, cache, degradation).

> Worked examples used MTL/Lucky (the available workbooks) rather than HUBC/ENGRO; the
> mechanism is company-agnostic via the `peers` registry + `COMPANY_TICKER` map.

---

## Phase 5 — Hardening

### Tasks
- **5.1** `trace.py`: persist `TraceRecord` (QueryFrame, SourcePlan, evidence, calcs, conflicts, citations) → replayable.
- **5.2** FastAPI route `POST /fie/answer` wiring the engine.
- **5.3** Eval harness: golden-query regression set + per-layer metrics.
- **5.4** Observability: structured logs per layer (reuse `app/core/logging.py`); surface `partial_coverage` and dropped-insight counts in responses (manifest/reconciliation stays a dev-harness signal, optionally shown as a caveat only).

**Exit criteria:** any answer is replayable from its `TraceRecord`; eval suite in CI.

### Implementation note (built)

- **Trace & replay** ([trace.py](../backend/app/engines/fie/trace.py)): `engine.answer_with_trace()`
  returns `(Response, TraceRecord)`; `TraceStore` persists/loads JSON (frame, plan, full
  evidence, response). Re-running a query is deterministic (no LLM), so a trace is both an
  audit record and a replay key.
- **API** ([api/routes/fie.py](../backend/app/api/routes/fie.py)): `POST /api/fie/answer`
  `{query, company?, audience?}` → `Response`; `GET /api/fie/companies`. Stores lazy-loaded
  & cached from `storage/outputs`; delivered workbooks auto-registered as peers for
  `peer_comparison`. Wired in `app/main.py`; tested via `TestClient`.
- **Eval harness** ([devtools/eval_harness.py](../backend/app/engines/fie/devtools/eval_harness.py)):
  7-case golden-query set with per-case expectations (intent, value≈, citations, findings,
  answer-contains) + per-intent metrics. `run_eval()` is green in CI (7/7).
- **Observability**: per-layer structured logging via the existing `app/core/logging.py`
  (`Understand | Retrieve | Respond` components), and a `Response.coverage` block exposing
  `degraded / partial_coverage / dropped_insights / superseded_insights / withheld`. Example:
  a `risk_assessment` query now reports `dropped_insights: 24, superseded_insights: 24` —
  the insight selection that was previously invisible. (Manifest/reconciliation stays a
  dev-harness signal, §0.3.)
- Final suite: **79 tests green** across Phases 0–5.

---

## Phase G — Golden fixtures for the **development-time validation harness**

> **Scope (§0.3):** these fixtures belong to the extraction-validation harness (tasks
> 2.8/2.9), **not** the runtime answer path. They certify that a delivered workbook's
> financial data is correct *before* the FIE trusts it. At answer time the engine
> assumes the figures are right and never consults face truths or the `Validation
> Ledger`. The face-truth values below come from the source PDFs and are therefore an
> OCR/extraction artifact — permissible for development validation, excluded from
> responses.

Derived from `ocr_millat_output_audit.md` (audited PDF face truths = source of truth).
**These define "done" for the validation harness** — they encode the exact failures that
made v5 `NOT_USABLE`, so the harness catches a bad workbook before it ever reaches users.

### G.1 Face-truth values — Millat (Rupees in thousand)

| Statement | Metric | Year | Face truth | Source |
|---|---|---:|---:|---|
| P&L | revenue | 2025 | 52,108,997 | millat-2025.pdf p108 |
| P&L | cost_of_sales | 2025 | −38,241,906 | p108 |
| P&L | gross_profit | 2025 | 13,867,091 | p108 |
| P&L | operating_profit | 2025 | 10,236,479 | p108 |
| P&L | finance_cost | 2025 | −2,172,644 | p108 |
| P&L | pat | 2025 | 6,372,928 | p108 |
| BS | total_equity | 2025 | 8,076,300 | p106–107 |
| BS | non_current_assets | 2025 | 8,014,208 | p106–107 |
| BS | current_assets | 2025 | 24,974,383 | p106–107 |
| BS | total_assets | 2025 | 32,988,591 | p106–107 |
| BS | total_equity_and_liabilities | 2025 | 32,988,591 | p106–107 |
| CF | cash_generated_from_operations | 2025 | 10,253,249 | p110 |
| CF | net_operating_cash_flow | 2025 | 3,341,999 | p110 |
| **Restated** | revenue | 2024 | 91,534,501 | 2025 rpt p108 |
| **Restated** | pat | 2024 | 10,224,875 | 2025 rpt p108 |
| **Restated** | total_assets | 2024 | 32,873,428 | 2025 rpt p106–107 |

### G.2 Derived-metric assertions (calc engine tie-outs)
- `gross_profit(2025)` == `revenue − cost_of_sales` == `52,108,997 − 38,241,906` == **13,867,091** ✓
- `gross_margin(2025)` == `13,867,091 / 52,108,997` ≈ **0.2661**
- Balance-sheet identity: `total_assets(2025)` == `total_equity_and_liabilities(2025)` == **32,988,591** ✓
- `total_assets(2025)` == `non_current_assets + current_assets` == `8,014,208 + 24,974,383` == **32,988,591** ✓

### G.3 Conflict fixtures (must be FLAGGED, not silently passed)

| Metric | Year | Bad v5 value | Face truth | Expected engine behavior |
|---|---:|---:|---:|---|
| revenue | 2025 | 57,840,150 | 52,108,997 | `MISMATCH` → confidence `Low` |
| revenue | 2024 | 57,222,177 | 91,534,501 | `restatement` conflict; prefer 91,534,501, explain |
| pat | 2025 | 12,165,035 | 6,372,928 | `MISMATCH` → `Low` |
| pat | 2024 | −21,956,716 | 10,224,875 | `MISMATCH` → `Low` |
| cash_and_bank | 2025 | 0 | 1,565,748 | `MISMATCH` → `Low` |
| total_assets | 2025 | 28,214,081 | 32,988,591 | `MISMATCH` → `Low` |

### G.4 Manifest-gate fixtures (confidence caps)
- Millat `fully_reconciled = false`, `validation_failures = 5` → any answer capped at **Medium** (or **Low** if a material input is `MISMATCH`/`WITHHELD`).
- Lucky `production_ready = true` **but** `fully_reconciled = false`, `validation_failures = 0` → capped at **Medium** (reconciliation gate dominates the production flag).

### G.5 Traceability fixture (the audit's #1 failure)
- For every headline `FactRef` rendered in a response, assert `source_ref` resolves to a `Source Ledger` row **or** the value is withheld. Zero unciteable numbers may appear in prose.

---

## Sequencing summary

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5
(foundation) (skeleton)  (det. core) (LLM)      (external)  (harden)
                 ▲            ▲
            lock interfaces   gated by Phase-G golden suite
```

Phase-G is authored in Phase 0 and **must pass before Phase 3** — the numeric core is
proven before any LLM prose can lean on it.
