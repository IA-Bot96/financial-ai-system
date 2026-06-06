# Financial Intelligence Engine (FIE) — Design & Architecture

**Status:** Design specification (no implementation)
**Scope:** Central reasoning and answer-generation layer of the financial analysis platform
**Grounding:** Designed against the actual extraction-pipeline output workbooks
(`millat_filled_fixed.xlsx`, `lucky_filled_fixed.xlsx`) and their manifests.

---

## 0. Grounding: what the engine actually consumes

The FIE does **not** start from raw PDFs. It sits on top of the extraction pipeline,
whose delivered artifact is a workbook with a fixed, known schema. Every design
decision below maps onto sheets that already exist. This is what makes the engine
auditable rather than aspirational.

### 0.1 Workbook contract (per company, per run)

| Layer | Sheets | Role for the FIE |
|---|---|---|
| **Input / detail** | `Mgmt info.`, `Qualtitative Data`, `PL1 – Revenue` … `PL7 – OCI`, `BS1 – Non-Current Assets` … `BS5 – Current Liabilities` | Line-item detail behind every headline number; the granular financial-data repository |
| **Output / statements** | `P&L`, `Balance Sheet` | Headline statements, multi-year columns (FY2021 → FY2025), the primary numeric surface users ask about |
| **Insight repository** | `Insights`, `Insights Review` | `Year · Source Report Year · Area · Takeaway · Source Section · Page · Confidence` — pre-extracted qualitative findings with per-row confidence |
| **Provenance** | `Source Ledger` | `Sheet · Cell · Template label · Matched label · Year · Value · Report year · Report file · Page · Table id` — **the citation backbone**: every value traces to `file:page:table` |
| **Conflict / quality** | `Validation Ledger` | `Status · … · Face truth · Source · Note`, statuses `WITHHELD` / `MISMATCH` / `NO_FACE_TRUTH` — **development-time validation signal only** (§0.3), not a runtime input |
| **Run metadata** | `*.manifest.json` | `production_ready`, `fully_reconciled`, `validation_failures`, … — **development-time trust signal** (§0.3), not a runtime input |

### 0.2 Why this grounding matters

The companion audit (`ocr_millat_output_audit.md`) records failure modes that arise in
the *extraction* pipeline. The FIE does **not** re-litigate them at answer time (see the
trust boundary in §0.3) — but they shape two things it still owns at runtime, and a
third it owns only during development:

- **Multi-year restatement** *(runtime)* — the FY2025 report restates FY2024 revenue
  (57.2bn as first reported vs 91.5bn restated). Both views can appear in the workbook's
  multi-year columns, so the engine must prefer the newest report's view of a prior year
  and surface the restatement, not silently pick one.
- **Traceability** *(runtime)* — the engine must **refuse to assert** any number it
  cannot cite to the in-workbook `Source Ledger`.
- **Extraction correctness** *(development only)* — face-statement tie-outs, the
  `Validation Ledger`, and the manifest reconciliation flags are used to *validate the
  extraction before delivery*. At answer time the delivered financial figures are
  treated as authoritative (§0.3); the audit-derived face truths become a
  **development-time validation harness**, never an input to a response.

### 0.3 Runtime input boundary & trust model

This is a hard architectural boundary, not a guideline.

**Runtime answer inputs are exactly three things:**

```
response = FIE( user_query , Excel workbook , external sources )
```

1. **User query** — the natural-language question.
2. **Excel workbook** — the delivered workbook only: statements (`P&L`, `Balance Sheet`),
   line-item detail (`PL*`, `BS*`), `Insights`/`Insights Review`, and the in-workbook
   `Source Ledger` (used for citations). This is the complete internal surface.
3. **External sources** — PSX / News / Macro / Market APIs and the forecast repository.

**Explicitly excluded from the answer path:** the source PDFs, OCR/debug artifacts,
face-truth values, and any other extraction byproduct. These exist **only for
development-time validation** of the extraction and never feed a user-facing response.

**Trust assumptions:**

- **Financial figures in the workbook are authoritative.** The engine does not recompute
  them to second-guess correctness; it *derives* new metrics from them (ratios, growth,
  valuation) and treats the stored values as ground truth. Consequently there is **no
  runtime "computed-vs-stated" financial conflict** and no manifest-reconciliation
  confidence cap — those are development concerns.
- **The `Source Ledger` is in-workbook**, so citations remain fully available at runtime.
- **The `Validation Ledger` / manifest flags are development signals.** They may be
  surfaced as an optional informational caveat, but they never override a trusted figure
  or gate a runtime answer.

These boundaries simplify the runtime engine: conflict detection and confidence at
answer time concern **insights and external data**, not the trusted financial core.

---

## 1. Engine architecture (high level)

```
                          ┌──────────────────────────────────────────────┐
   user_query ──────────► │            FINANCIAL INTELLIGENCE ENGINE       │
   + workbook(s)          │                                                │
                          │  1. Query Understanding  ─────────────┐       │
                          │        │ intent + entities             │       │
                          │        ▼                               │       │
                          │  2. Source Selection (planner)         │       │
                          │        │ source plan                   │       │
                          │        ▼                               │       │
                          │  ┌───────────────┐  ┌────────────────┐ │       │
                          │  │ Internal       │  │ 3. API          │ │       │
                          │  │ Retrieval      │  │ Orchestration   │ │       │
                          │  │ (workbook)     │  │ (PSX/News/Macro)│ │       │
                          │  └──────┬─────────┘  └────────┬───────┘ │       │
                          │         └─────────┬───────────┘         │       │
                          │                   ▼                     │       │
                          │  4. Financial Calculation Engine        │       │
                          │        │ (formula registry)             │       │
                          │        ▼                                │       │
                          │  5. Evidence Synthesis  ◄───────────────┘       │
                          │        │                                        │
                          │        ▼                                        │
                          │  7. Conflict Detection & Resolution             │
                          │        │                                        │
                          │        ▼                                        │
                          │  8. Confidence Assessment                       │
                          │        │                                        │
                          │        ▼                                        │
                          │  6/9. Citation binding + Response Generation    │
                          └──────────────────────┬─────────────────────────┘
                                                  ▼
                                              response
   (direct answer · key findings · analysis · calculations · evidence · citations · confidence)
```

### 1.1 Layered component view

| # | Layer | Responsibility | Key state |
|---|---|---|---|
| L0 | **Ingestion / Indexing** | Load workbook(s); build typed in-memory model; index findata + `Source Ledger` by `(metric, year)` / `cell` (the `Validation Ledger` is loaded only for the dev validator, §0.3) | `FinancialFactStore` |
| L1 | **Query Understanding** | Intent classification + entity extraction | `QueryFrame` |
| L2 | **Source Selection / Planner** | Map intent → required sources + tools | `SourcePlan` (a DAG) |
| L3 | **Retrieval & Orchestration** | Execute internal lookups + external API calls | `EvidenceItem[]` |
| L4 | **Calculation Engine** | Apply formula registry to retrieved facts | `CalcResult[]` (with input citations) |
| L5 | **Synthesis & Reasoning** | Merge evidence + calcs into a reasoned conclusion | `ReasoningGraph` |
| L6 | **Conflict & Confidence** | Detect contradictions, score trust | `ConflictSet`, `ConfidenceReport` |
| L7 | **Response Generation** | Render structured, cited answer | `Response` |
| L8 | **Audit / Trace store** | Persist the full reasoning trace for replay | `TraceRecord` |

**Design principle — single fact identity.** Every number that flows through the
engine carries an immutable `FactRef` from the moment it is read:

```
FactRef = {
  company, sheet, cell, metric, year,
  value, unit ("Rupees in thousand"),
  source_ref = "<report_file>:p<page>:<table_id>",   # from Source Ledger
  report_year,                                         # which report this came from
  validation_status,                                  # from Validation Ledger, or CLEAN
  face_truth                                           # audited value if known
}
```

A `FactRef` is never stripped of its provenance. Citations (§6) and confidence (§8)
are *read off* the `FactRef`, not reconstructed later. This is the architectural
answer to the audit's traceability finding.

---

## 2. Query Understanding & Intent Analysis

### 2.1 Pipeline

```
query → normalize → intent classify → entity extract → temporal resolve → QueryFrame
```

A hybrid classifier (rules + LLM) is used: deterministic keyword/grammar rules give a
high-precision first pass and cheap explainability; the LLM handles paraphrase and
ambiguity. The LLM is constrained to emit a typed `QueryFrame` (function/tool-call
schema), never free text, so downstream stages get structured input.

### 2.2 Intent taxonomy (closed set, extensible)

| Intent | Trigger examples | Primary sources |
|---|---|---|
| `performance_analysis` | "how did revenue do" | `P&L`, `Balance Sheet`, calcs |
| `ratio_analysis` | "what's the current ratio" | `BS*`, `PL*`, formula registry |
| `valuation` | "is it cheap", "fair value" | statements + PSX price |
| `forecast_validation` | "is my 2026 forecast still valid" | Forecast repo + statements + News + PSX |
| `peer_comparison` | "MTL vs Lucky" | multiple workbooks |
| `trend_analysis` | "margin trend over 5 years" | multi-year columns |
| `risk_assessment` | "key risks" | `Insights` (Area = risk) |
| `earnings_review` | "review latest earnings" | `P&L` + News + announcements |
| `dividend_analysis` | "dividend sustainability" | cash flow + `Insights` |
| `news_impact` | "how does X news affect" | News API + statements |
| `scenario_analysis` | "what if rates rise 2%" | Macro + calcs |
| `data_quality` | "is this number reliable" | *diagnostic/dev intent* — may expose `Validation Ledger`/manifest as a caveat (§0.3); financial figures are otherwise trusted |

### 2.3 Entity extraction

Extract and normalize: **company** (→ ticker, e.g. HUBC, LUCK, MTL, ENGRO),
**sector**, **metric** (→ canonical metric id, mapped against `Source Ledger` matched
labels), **time period** (→ explicit FY list), **comparison targets**, **forecast
references**, **external-data requirements**.

### 2.4 Temporal resolution (critical, restatement-aware)

"2024 revenue" is ambiguous because the workbook holds FY2024 *as reported in the 2024
report* and *as restated in the 2025 report*. The resolver expands every period into:

```
{ fiscal_year: 2024, report_year_preference: "latest" }   # default
```

and records the alternative. This is where the audit's restatement failure is
defended at the *understanding* stage, before retrieval.

### 2.5 Worked example

```
Query: "Has my 2026 earnings forecast for HUBC remained valid?"

QueryFrame {
  intent: forecast_validation,
  entities: { company: HUBC, metric: EPS/earnings, fiscal_year: 2026 },
  required_sources: [ ForecastRepository, FinancialData(P&L),
                      Insights, NewsAPI, PSX_MarketData ],
  temporal: { horizon: 2026, latest_actual: most_recent_FY },
  comparison: actual_vs_forecast
}
```

---

## 3. Source Selection Framework

### 3.1 Source registry

Each source is declared once, with a capability descriptor the planner reasons over:

```
SourceDescriptor {
  id, kind: internal|external,
  provides: [metric classes / data types],
  granularity, freshness_sla, reliability_rating (0–1),
  cost, auth, access_path
}
```

**Internal sources** (from the workbook):

| Source | Backed by | Provides |
|---|---|---|
| `FinancialData` | `P&L`, `Balance Sheet`, `PL1–7`, `BS1–5` | statements + line-item detail (**authoritative**, §0.3) |
| `Insights` | `Insights`, `Insights Review` | trends, risks, opportunities, mgmt commentary |
| `Provenance` | `Source Ledger` | citations for any value |
| `ForecastRepo` | (platform forecast store) | revenue/EPS/margin/valuation forecasts, versioned |

> The `Validation Ledger` / manifest are **not** a runtime source (§0.3) — they belong to
> the development-time validation harness and may only appear as an optional caveat.

**External sources:** PSX APIs (profiles, results, announcements, corporate actions,
market stats), News APIs (company/sector/economic), Macro APIs (rates, inflation, FX,
GDP), Market data (price, volume, industry).

### 3.2 Selection logic

Intent → a declarative requirement set → matched against `provides`. Selection rules:

1. **Always bind provenance** for any internal numeric source (non-optional) — every
   figure must remain citable to the `Source Ledger`.
2. **Recency-sensitive intents** (`forecast_validation`, `news_impact`,
   `earnings_review`, `valuation`) require at least one *external freshness* source;
   if unavailable, the engine degrades gracefully and lowers confidence (§8) rather
   than failing.
3. **Cost/latency budgeting** — the planner picks the minimum source set that covers
   the requirement; optional enrichers are added only if budget allows.
4. **Source sufficiency check** — if required metrics can't be covered, the planner
   emits a `partial_coverage` flag carried into the response.

### 3.3 Output: a `SourcePlan` DAG

Nodes = retrieval/calc/API tasks; edges = data dependencies. The DAG enables parallel
independent fetches and ordered dependent ones (e.g., fetch price *before* computing
P/E). The plan is itself stored in the trace for auditability.

### 3.4 Insight selection & ranking

Insights are not consumed wholesale — they are **selected by relevance to the query**
and then **deconflicted**. This runs at retrieval time and feeds synthesis (§6).

**Step 1 — Relevance selection.** From the `Insights` JSON records, select those
matching the `QueryFrame`:

- **entity/topic match** — `Area` and `Takeaway` matched against the query's metric /
  topic (keyword + embedding similarity);
- **temporal match** — `Year` within the query's resolved period (or the most recent
  available when the query is point-in-time);
- **intent affinity** — e.g., `risk_assessment` favors risk-themed `Area`s.

Only insights above a relevance threshold enter the candidate set; the rest are dropped
(and the count of dropped/*available-but-unused* insights is logged for auditability).

**Step 2 — Conflict resolution among selected insights.** When two selected insights
make contradictory claims about the same topic, resolve by **recency first, confidence
second** (configurable):

```
Default (year-dominant, lexicographic):
  winner = argmax over (Year, then Confidence)
  # newer report year always wins; Confidence breaks ties within the same year

Alternative (blended score), when years are close or a smooth trade-off is wanted:
  score(insight) = α · recency_norm(Year) + (1 − α) · Confidence
  recency_norm(Year) = (Year − min_year) / (max_year − min_year)
  default α = 0.7   # year-weighted, so recency still dominates but a much
                    # higher-confidence older insight can outrank a marginally newer one
```

`α = 1.0` reproduces strict year-priority; `α = 0.0` is pure confidence. The chosen mode
and `α` are recorded in the trace. The losing insight is **not silently discarded** — it
is retained as a superseded alternative and may be surfaced as a caveat (e.g., "an older
FY2023 note suggested the opposite"). Insight conflict resolution is independent of the
financial core, which is trusted as-is (§0.3).

---

## 4. API Orchestration Layer

### 4.1 Responsibilities

select → build request → execute → parse → validate → normalize → merge.

### 4.2 Per-API contract (documented for every external API)

```
ApiSpec {
  purpose, endpoint, method, parameters, response_schema,
  refresh_frequency, reliability_rating, auth, rate_limit,
  normalizer: (raw) → [EvidenceItem with FactRef],
  failure_mode: cache | degrade | omit
}
```

Example (illustrative):

```
ApiSpec PSX.CompanyFinancials {
  purpose: "latest reported results / market stats for a ticker",
  endpoint: GET /psx/v1/companies/{ticker}/financials,
  parameters: { ticker, period },
  response_schema: { eps, pat, revenue, as_of_date, ... },
  refresh_frequency: "intraday for price, quarterly for results",
  reliability_rating: 0.9,
  normalizer: maps each field → EvidenceItem{
     source_ref = "PSX:CompanyFinancials:{retrieved_at}", reliability: 0.9 },
  failure_mode: degrade   # proceed on internal data, lower confidence
}
```

### 4.3 Orchestration mechanics

- **Parallelism & ordering** driven by the `SourcePlan` DAG.
- **Resilience:** timeout, retry-with-backoff, circuit-breaker per API; cached
  last-good response with explicit `retrieved_at` staleness.
- **Normalization to `EvidenceItem`:** every external datum becomes a first-class
  `EvidenceItem` carrying its own `source_ref`, `retrieved_at`, `reliability_rating` —
  so external data is cited and conflict-checked exactly like internal data.
- **Unit & scale reconciliation:** PSX values (often absolute PKR) are normalized to
  the workbook's "Rupees in thousand" before any merge or calculation.

---

## 5. Financial Reasoning & Calculation Engine

### 5.1 Principle: trust the workbook figures, derive everything else

Per the trust boundary (§0.3), the workbook's financial figures are **authoritative** at
runtime. The engine does not recompute headline totals to second-guess them; it reads
stored values directly and uses the formula registry to **derive** metrics the workbook
doesn't already carry — ratios, growth rates, margins, valuation multiples. A derived
metric inherits its inputs' citations and confidence.

If a needed value already exists in the workbook (e.g., `gross_profit`), the engine
prefers the stored value over recomputation; it computes from line-item detail
(`PL*`, `BS*`) only to *fill a gap*, not to *audit* a stated number. (Recompute-and-
cross-check against face truth still exists — but as the **development-time validation
harness**, §0.2 / Phase G of the implementation plan, never as a runtime gate.)

### 5.2 Formula Registry design (deliverable #7)

A declarative, versioned registry — formulas are data, not code, so new metrics are
added without touching the engine.

```
FormulaSpec {
  id: "gross_margin",
  category: profitability,
  expression: "gross_profit / revenue",
  inputs: [ {metric: gross_profit, required}, {metric: revenue, required} ],
  domain_guards: [ revenue != 0 ],
  unit_policy: ratio|currency|percent,
  output_unit, rounding,
  citation_policy: "cite every input FactRef",
  version
}
```

Each `inputs` entry resolves through the **metric ontology** to a concrete `FactRef`
(sheet/cell/year). The evaluator:

1. resolves inputs to `FactRef`s (recording provenance),
2. enforces `domain_guards` (e.g., no divide-by-zero, sign conventions — note
   `Cost of sales` is stored negative in `P&L`),
3. evaluates, attaches **all input citations** to the `CalcResult`,
4. tags the result with the **minimum confidence of its inputs**.

### 5.3 Registry coverage (initial)

| Category | Formulas |
|---|---|
| Growth | revenue growth, earnings growth, CAGR, segment growth |
| Profitability | gross/operating/EBITDA/net margin, ROA, ROE, ROIC |
| Liquidity | current, quick, cash ratio |
| Leverage | debt-to-equity, debt-to-assets, interest coverage |
| Cash flow | operating CF, free CF, FCF yield |
| Valuation | P/E, P/B, EV/EBITDA, DCF, Graham, DDM |
| Forecast validation | forecast error, accuracy, variance, actual-vs-forecast |

### 5.4 Calculation provenance example

```
ROE FY2024 = PAT_2024 / avg_equity_2024
  PAT_2024      → P&L!F24       src millat-2025.pdf:p108  (validation: CLEAN)
  equity_2024   → Balance Sheet!Fxx  src millat-2025.pdf:p106
  equity_2023   → Balance Sheet!Exx  src millat-2024.pdf:p113
  result: 0.42   confidence: min(inputs) = High
```

---

## 6. Multi-Source Evidence Synthesis

### 6.1 Evidence model

All retrieved data — internal cells, insights, external API results, and `CalcResult`s
— are normalized to a common `EvidenceItem`:

```
EvidenceItem {
  claim,                       # the assertion ("FY24 revenue = 91,534,501")
  value, unit,
  kind: statement|detail|insight|external|calc,
  fact_refs: [FactRef...],     # provenance
  reliability, freshness, as_of
}
```

### 6.2 Synthesis workflow (deliverable #8)

```
gather → align (by metric + period + entity) → corroborate → weigh → reason → conclude
```

1. **Align** evidence into clusters keyed by `(entity, metric, fiscal_year)`.
2. **Corroborate:** within a cluster, do independent sources agree? (e.g., PSX EPS vs
   the workbook's authoritative EPS — divergence flags the *external* value, never the
   workbook one; §0.3.)
3. **Weigh** by reliability × freshness (financial workbook figures carry top reliability
   by assumption; insights weighed by §3.4 recency/confidence).
4. **Reason** along an explicit `ReasoningGraph` (premises → inferences → conclusion),
   where every premise node points to `EvidenceItem`s. This graph *is* the
   "Supporting Analysis" section of the response and the audit trail.
5. **Conclude**, carrying forward any unresolved conflicts to §7 and the aggregate
   confidence to §8.

### 6.3 Worked example — "Is ENGRO likely to achieve management guidance?"

Evidence cluster assembled: historical guidance-vs-actual accuracy (Forecast repo),
YTD performance (`P&L`), revenue & margin trend (multi-year columns), management
commentary (`Insights` where Area ∈ {guidance, outlook}), recent announcements (PSX),
industry conditions (Macro/News). Synthesis weighs trend + guidance track record
against macro headwinds and emits a probabilistic, evidence-linked conclusion.

---

## 7. Citation & Traceability System

### 7.1 Mapping the spec onto the actual ledger

The required citation styles map **directly** onto `Source Ledger` columns:

| Spec citation type | Realized as |
|---|---|
| Financial data (Workbook/Sheet/Cell) | `Source Ledger`: `Sheet · Cell · Year · Value` |
| Report (Annual Report / Page / Section) | `Source Ledger`: `Report file · Page · Table id` (+ `Insights`: `Source Section · Page`) |
| Insight ID / category | `Insights` row id + `Area` |
| Forecast ID / version | Forecast repo `forecast_id · version` |
| External source | `EvidenceItem.source_ref` = `PSX:<endpoint>:<retrieved_at>` |

### 7.2 Citation binding rule

**No number is rendered without a resolvable citation.** At render time the engine
walks every `FactRef`/`source_ref` in the answer and resolves it against the
`Source Ledger` (internal) or the API normalizer log (external). A claim whose
citation cannot be resolved is **withheld** and reported as a coverage gap — directly
addressing the audit's "4,100/4,100 values with null source" finding.

### 7.3 Citation object

```
Citation {
  ref_id,                       # stable handle used inline, e.g. [C7]
  kind, display,                # human string: "Annual Report 2025, p108, Income Statement"
  locator: { report_file, page, table_id, sheet, cell },
  retrieved_at,                 # external only
  validation_status             # surfaced so reader sees data quality
}
```

Every numeric output and every factual sentence links to ≥1 `Citation`.

---

## 8. Conflict Detection & Resolution

Scope note (§0.3): the financial core is trusted, so there is **no runtime
"computed-vs-stated" conflict** and the `Validation Ledger` is not consulted at answer
time. Runtime conflicts concern **insights, external data, forecasts, and
restatements visible within the workbook** — not the correctness of stored figures.

### 8.1 Conflict types detected at query time

| Type | Example | Detection | Resolution |
|---|---|---|---|
| **Insight vs insight** | two `Insights` rows contradict on the same topic | same topic/`Area`, opposing `Takeaway` | **year → confidence** (§3.4) |
| Restatement | FY2024 revenue as-first-reported vs restated | same `(metric, year)`, different `report_year` *within the workbook* | newest report year |
| Forecast vs actual | 2026 EPS forecast vs reported | Forecast repo vs `P&L` | report the variance (both are valid facts) |
| Internal vs external | workbook EPS vs PSX EPS | `EvidenceItem` corroboration | workbook authoritative; flag external divergence |
| Cross-API | two APIs differ | reliability-ranked | higher `reliability_rating`, fresher |
| Insight vs disclosure | insight contradicts latest announcement | `Insights` vs PSX/News, date-aware | fresher external disclosure |

### 8.2 Resolution policy

```
detect → classify → rank → resolve or expose
```

Ranking precedence (default, configurable):

1. **Workbook financial figure** — authoritative for any *financial value* (§0.3); an
   external source that disagrees is flagged, not used to overwrite it.
2. **Newest report year** for a restated prior year (restatement preference).
3. **Insight recency, then confidence** (§3.4) for insight-vs-insight conflicts.
4. **Higher `reliability_rating`**, then **fresher** data, for external/cross-API.

When precedence is decisive → resolve and **explain why** ("FY2024 revenue uses the
FY2025 restatement"). When not → **present the discrepancy with both values and
their citations** and lower confidence. The engine never silently picks one.

---

## 9. Confidence Assessment

### 9.1 Inputs to the score

Confidence at runtime is about **insight strength and external corroboration**, not the
correctness of the financial core (trusted by assumption, §0.3).

| Factor | Signal source |
|---|---|
| Financial inputs | trusted as authoritative (§0.3) — contribute high base confidence, not re-scored |
| External source quality | `reliability_rating` of PSX/News/Macro |
| Source freshness | `retrieved_at` vs metric volatility |
| Data completeness | `partial_coverage` flag (required metric/source unavailable) |
| Insight strength | selected insights' `Confidence` and `Year` recency (§3.4) |
| Cross-source agreement | §6.2 corroboration; §8 conflicts |
| Forecast uncertainty | forecast horizon + variance history |

### 9.2 Scoring model

A transparent weighted-rubric score (not a black box), aggregated to a band:

```
confidence = weighted_combine(per-factor sub-scores)
band: High | Medium | Low

Rules (hard caps):
  - answer leans on a low-confidence / superseded insight (§3.4)  → cap at Medium
  - required external freshness source missing                    → cap at Medium
  - answer relies on a single uncorroborated source               → at most Medium
  - an unresolved conflict bears on the answer                     → cap at Medium
# Note: there is NO financial-mismatch / reconciliation cap at runtime — those are
# development-time validation concerns (§0.2, Phase G), not answer-path signals.
```

Confidence is computed **bottom-up**: each `EvidenceItem`/`CalcResult` carries a
confidence; the answer's confidence is the aggregation, so it is explainable per claim.

### 9.3 Output

```
Confidence: High
Reason:
 - Financial figures sourced from the workbook (authoritative), cited to Source Ledger
 - Supporting insight is FY2025, confidence 0.95 (most recent, no contradiction)
 - No conflicting disclosures in News/PSX for the period
```

---

## 10. Response Generation

### 10.1 Structure (always the same skeleton)

1. **Direct Answer** — one or two sentences.
2. **Key Findings** — bullets, each with inline citation `[C1]`.
3. **Supporting Analysis** — the `ReasoningGraph` narrated.
4. **Calculations** — formula, inputs (with citations), result.
5. **Evidence Used** — sources consulted (internal sheets + external APIs).
6. **Citations** — resolved `Citation` list.
7. **Confidence Assessment** — band + reasons (optional/configurable).

### 10.2 Generation discipline

- The LLM writes prose **only from the structured `ReasoningGraph`, `CalcResult`s, and
  `EvidenceItem`s** — it does not invent numbers. Every figure it emits must match a
  `FactRef` already in scope (validated at render via §7.2).
- **Audience modes:** `analyst` (full detail) vs `investor` (concise, plain-language)
  toggle verbosity, not facts.
- **Refusal/abstention:** if coverage is `partial` or all evidence for a needed metric
  is `WITHHELD`, the engine says so explicitly rather than guessing.

---

## 11. End-to-End Execution Workflow

```
1. Ingest      → load workbook; build FinancialFactStore (findata+Source Ledger); index by (metric, year)
2. Understand  → QueryFrame (intent + entities + temporal, restatement-aware)
3. Plan        → SourcePlan DAG (internal + external, budgeted)
4. Retrieve    → internal lookups (FactRefs, trusted) + insight selection ∥ external API calls (EvidenceItems)
5. Calculate   → formula registry; derive metrics from authoritative workbook values
6. Synthesize  → align, corroborate, weigh → ReasoningGraph
7. Reconcile   → runtime conflict detection (insights, restatement, external) + resolution
8. Score       → bottom-up confidence (insight/external/coverage caps; no financial cap)
9. Cite        → bind & resolve every FactRef/source_ref; withhold the unciteable
10. Respond    → render structured answer (audience-aware)
11. Trace      → persist QueryFrame, SourcePlan, evidence, calcs, conflicts, citations → replayable
```

Step 11 makes every answer **replayable**: the stored `TraceRecord` lets a reviewer
reconstruct exactly which cells, reports, pages, and API responses produced the answer.

---

## 12. Extensibility Strategy

The engine is built so that **the four things that change most often are data, not
code**:

| To add… | You register… | No engine change because… |
|---|---|---|
| **New API** | an `ApiSpec` (+ normalizer to `EvidenceItem`) | orchestration is spec-driven; planner discovers it via `provides` |
| **New formula/metric** | a `FormulaSpec` in the registry | evaluator is generic; inputs resolve via metric ontology |
| **New data source** | a `SourceDescriptor` | planner matches `provides` to requirements |
| **New intent** | an entry in the intent taxonomy + a planner rule | classifier and planner are table-driven |
| **New model** | swap the model id behind the classifier/generator interface | LLM is behind a typed boundary (structured I/O), not woven through logic |

Cross-cutting extensibility guarantees:

- **Metric ontology** decouples report wording ("Furniture and office equipment") from
  canonical metric ids — already evidenced by `Source Ledger`'s
  `Template label` vs `Matched label` columns. New label variants extend the ontology,
  not the code.
- **Versioning** on formulas, API specs, and forecasts → reproducible historical
  answers even as definitions evolve.
- **Reliability ratings & resolution precedence** are configuration, so adding a more
  authoritative source automatically improves conflict resolution.

---

## 13. Mapping: deliverables → sections

| # | Deliverable | Section |
|---|---|---|
| 1 | FIE architecture | §1 |
| 2 | Query understanding framework | §2 |
| 3 | Intent classification framework | §2.2 |
| 4 | Source selection framework | §3 |
| 5 | API orchestration framework | §4 |
| 6 | Financial calculation engine | §5 |
| 7 | Formula registry design | §5.2 |
| 8 | Multi-source reasoning workflow | §6 |
| 9 | Evidence synthesis framework | §6 |
| 10 | Citation & traceability system | §7 |
| 11 | Conflict resolution methodology | §8 |
| 12 | Confidence scoring methodology | §9 |
| 13 | Response generation framework | §10 |
| 14 | End-to-end execution workflow | §11 |
| 15 | Extensibility strategy | §12 |

---

## 14. Design tenets (summary)

1. **Provenance is intrinsic, not appended** — `FactRef` travels with every number.
2. **Trust the workbook, derive the rest** — financial figures are authoritative at
   runtime (§0.3); the engine derives metrics from them and validates the *extraction*
   separately, off the answer path (Phase G).
3. **Three inputs only** — a response draws on the user query, the workbook, and
   external sources; no OCR byproducts ever enter the answer path.
4. **Conflicts are surfaced, not hidden** — insight conflicts (year→confidence),
   restatements, and external divergences are first-class outputs.
5. **Confidence is bottom-up and capped** — insight strength, freshness, and coverage
   bound the ceiling (no financial-mismatch cap at runtime).
6. **No citation, no claim** — unciteable numbers are withheld.
7. **Specs over code** — APIs, formulas, sources, and intents extend by registration.
8. **Every answer is replayable** — the trace reconstructs the reasoning end to end.
```

