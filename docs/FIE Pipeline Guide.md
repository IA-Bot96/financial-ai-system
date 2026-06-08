# FIE Pipeline — Layer Guide (Workbook + Question → Cited Answer)

A companion to the FIE pipeline diagram (`FIE Pipeline.svg`). For each layer: **what
happens (plain English)**, **key terms explained**, and the **exact structure of its
output** (grounded in the real `app/engines/fie/` models).

> **How to read the flow:** Layer 1 runs **once** when a workbook is uploaded (a "session").
> Layers 2–8 then run for **every question** asked against that session.
> `.xlsx + question → FactStore → QueryFrame → SourcePlan → EvidenceItem[] → Calcs+ctx →
> Roled evidence + Conflicts → Response → Answer + trace.json`

---

## Layer 1 — Session Ingest   ·   *Rule-Based*

**What happens:** When the user uploads the workbook, FIE parses it **once** into a
fast, queryable **fact store** and keeps it resident for the whole session, so later
questions never re-read the file.

**Terms:**
- **Fact store** — the workbook flattened into a table of facts (one row per
  metric/year/cell) you can look up instantly.
- **Sheet role** — what each tab *is*: `statement` (P&L/BS/CF), `detail` (breakdown notes),
  `source_ledger` / `validation_ledger` (the audit sheets), `insights`. Drives which sheets
  are editable in the UI.
- **Provenance** — each fact remembers its origin (sheet, cell, and the source report/page
  via the ledgers) so answers can be cited.
- **Available metrics** — the set of metrics that actually have values; used later to match
  query terms and to gate "we don't have that" answers.

**Output — session descriptor + resident `FinancialFactStore`:**
```
API response:
  session_id   "a1b2c3…"            # 16-char id; the engine is cached under this
  company      "Lucky Cement Limited"
  years        [2023, 2024, 2025]
  sheets       [ {name, role, editable} … ]   # role ∈ statement|detail|ledger|insights…
  metrics      ["revenue","gross_profit","total_assets", …]   # available_metrics()

FinancialFactStore (in-memory):
  findata: DataFrame  columns =
     [company, statement(pl|bs|cf|equity|other), level(headline|detail),
      sheet, cell, label, section, metric, note_ref, year, period_type, value]
  .lookup(metric, year, level, period_type) -> FactRef
  .cite(fact) -> [Citation]
  .available_metrics(), .years
```

---

## Layer 2 — Query Understanding   ·   *Hybrid (Rules + LLM)*

**What happens:** Convert the free-text question ("What was gross profit in 2025?") into a
**structured intent** the rest of the engine can act on — what's being asked, which metric,
which year, which company, which formula.

**Terms:**
- **Intent** — the *kind* of question (one of 13): `metric_lookup`, `ratio_analysis`,
  `trend_analysis`, `overview`, `metric_comparison`, `peer_comparison`, `driver_analysis`,
  `risk_assessment`, `valuation`, `dividend_analysis`, `news_impact`, `earnings_review`,
  `forecast_validation`, or `unknown`.
- **QueryFrame** — the structured result (intent + extracted entities).
- **Ontology / canonical metric** — a dictionary that maps the many ways people say a metric
  ("turnover", "sales", "topline") to one **canonical** id (`revenue`). Built from ~90 seed
  aliases + a 419-metric registry, specialised to *this* workbook's labels.
- **Follow-up** — a short continuation ("and 2024?", "what about ROE?"). The **LLM** uses the
  chat **history** to fill in what the user left implicit (e.g. keep the previous metric).
- **Rules vs LLM** — ~95% is deterministic regex/keyword rules; the LLM is one optional call
  *only* to resolve follow-ups against history.

**Output — `QueryFrame`:**
```
QueryFrame:
  raw_query                "what was gross profit in 2025?"
  intent                   metric_lookup | ratio_analysis | … | unknown
  company / companies[]    resolved target(s)
  year / years[] / window  e.g. 2025  ·  [2021..2025]  ·  "last 3 years"
  formula                  "current_ratio" | "roe" | … | null   (for ratio_analysis)
  metrics                  ["gross_profit"]                      (canonical ids)
  level                    headline | detail        = headline
  period_type              historical | forecasted  = historical
  report_year_preference   latest | as_reported     = latest
  source                   rules | llm              # who produced this frame
```

---

## Layer 3 — Planning & Source Routing   ·   *Rule-Based*

**What happens:** Given the intent, decide **where** to get the answer — the workbook only,
external APIs, or both — and which specific APIs are worth calling.

**Terms:**
- **SourcePlan** — the shopping list of what to fetch (internal facts + external sources).
- **Internal vs external** — *internal* = the loaded workbook; *external* = web APIs (news,
  stock prices, dividends, analyst forecasts).
- **Registry / shortlist** — there's a catalogue of ~17 external APIs; for news/risk/earnings
  questions a `shortlist(query)` step picks only the few most relevant ones.
- **Budget cap** — a hard ceiling on external calls per question (`max_external_calls_per_request`).

**Output — `SourcePlan`:**
```
SourcePlan:
  requirements: [ { kind: internal|external, metric, year, level, period_type } ]
  formula            "current_ratio" | null
  external_sources   ["news","psx","forecast","company_payouts", …]
  registry_apis      ["company_announcements","analysis_reports","secp_notices", …]
  notes              [ … ]   # audit log of routing decisions
```

---

## Layer 4 — Retrieval (Internal + External)   ·   *Hybrid (Workbook + APIs)*

**What happens:** Actually fetch the data — look facts up in the workbook (with citations),
and call the planned external APIs — and wrap everything as uniform **evidence**.

**Terms:**
- **EvidenceItem** — one piece of supporting data (a workbook fact or an external datum),
  with its value, its citation(s), and a reliability score.
- **FactRef** — the immutable identity of a workbook fact (where it lives + its value).
- **Citation** — a resolvable pointer back to the source (cell, page, or external URL +
  retrieval time).
- **Reliability** — trust weight: internal workbook facts = 1.0; external = ~0.8–0.95 per API.
- **Resilient HTTP** — the external client survives flaky networks: **retry** with backoff,
  a **circuit-breaker** (stop hammering a failing API), **cache** (reuse last-good), and an
  **SSRF guard** (block unsafe URLs).

**Output — `list[EvidenceItem]`:**
```
EvidenceItem:
  claim         "gross_profit 2025 = 13,967,091"
  value         13967091.0 | null
  unit          "Rupees in thousand"
  kind          statement | detail | external
  reliability   0.0–1.0       # 1.0 internal; <1 external
  role          (set in Layer 6)
  fact_refs:  [ FactRef ]
      FactRef:
        company, metric, label, year, period_type
        value, unit, statement(pl|bs|cf|equity|other), level(headline|detail)
        sheet, cell, source_ref ("<report>:p<page>:<table_id>"), report_year,
        provenance_basis (direct | via_detail | workbook | none)
  citations:  [ Citation ]
      Citation: ref_id("C1"), kind(internal|external), display, locator, retrieved_at
```

---

## Layer 5 — Calculation & Intent Handlers   ·   *Rule-Based (+ LLM agent fallback)*

**What happens:** Compute any derived numbers (ratios, growth, decompositions) and run the
**handler** for this intent, which assembles the evidence/calcs/insights that the answer
will use. If the question is unrecognised or the workbook has nothing, an **LLM agent**
composes an answer by calling safe tools.

**Terms:**
- **Formula / CalcResult** — a derived metric (e.g. `current_ratio = current_assets /
  current_liabilities`) and the facts it used (so it stays cited).
- **Intent handler** — a small routine per intent (`metric_lookup`, `ratio_analysis`,
  `overview`, `trend`, `risk_assessment`, …) that gathers the right evidence.
- **Agentic fallback** — when intent is `unknown` or no internal data was found, an LLM
  loop is allowed to call **deterministic tools** (`get_value`, `compute_ratio`, `growth`,
  `insights`, `external_search`). Hard rule: it may **never state a number that didn't come
  from a tool** (no invented figures).
- **ctx** — the working context the handler fills: evidence, calcs, selected insights, flags.

**Output — `CalcResult[]` + handler context (`ctx`):**
```
CalcResult:
  formula_id   "current_ratio"
  value        1.83 | null
  unit         "ratio" | "percent" | "x"
  inputs       [ FactRef ]      # the facts used (for citation)
  citations    [ Citation ]
  expression   "current_assets / current_liabilities"
  note         error reason | null

ctx (internal):
  evidence[]  calcs[]  conflicts[]  selected_insights[]
  extra{ ratio_series | overview_items | driver list | clarify … }
  degraded  partial_coverage  trace_id
```

---

## Layer 6 — Evidence Trust & Conflicts   ·   *Hybrid (Rules + Embeddings)*

**What happens:** Make the evidence trustworthy: rank news by relevance, tag each item with
its **role** (how authoritative it is), and detect/resolve **conflicts** between sources.

**Terms:**
- **News semantic retrieval** — long articles are split into **chunks**, each chunk is turned
  into a vector by a small **local embedding model (BGE)**, ranked by similarity to the
  question (+ recency), and near-duplicates (wire-service syndication) are removed.
- **Admission / role** — every evidence item is stamped with a role:
  `baseline` (the audited workbook — highest authority), `supporting` (external corroboration),
  `event_fact` (a dated disclosure), `forecast` (analyst projection — lower weight),
  `narrative` (commentary), `auxiliary` (macro/context).
- **Conflict** — two sources disagree. Types: `restatement` (a year revised across reports),
  `forecast_vs_actual`, `internal_vs_external`, `cross_api`, `insight_vs_insight`,
  `insight_vs_disclosure`.
- **Authority matrix** — the rule for *who wins* a conflict: baseline > supporting > forecast
  > auxiliary. Conflicts that can't be resolved are **exposed** to the user, not hidden.

**Output — roled `EvidenceItem[]` + `Conflict[]`:**
```
EvidenceItem.role  ∈ baseline | supporting | event_fact | forecast | narrative | auxiliary

Conflict:
  type         restatement | forecast_vs_actual | internal_vs_external | cross_api | …
  topic        "revenue"
  year         2024 | null
  values     [ { source, value, unit, authority, year, takeaway } … ]   # competing claims
  resolution   "winner + rationale" | null
  resolved     true/false        # false -> surfaced to the user
```

---

## Layer 7 — Confidence & Response   ·   *Hybrid (Template + LLM)*

**What happens:** Score how much to trust the answer, then render a structured, **cited**
response. An LLM writes the prose narrative — but is forbidden from introducing any number
not already in the evidence.

**Terms:**
- **Confidence (weakest-link)** — a 0–1 score and a band (**High / Medium / Low**) computed
  from evidence quantity, freshness, diversity, role mix, conflicts, and coverage. The final
  score is the **minimum** of the components — the weakest factor caps it (and is named).
- **7-section answer** — direct answer, key findings, supporting analysis, calculations,
  citations, conflicts, confidence.
- **Citation enforcement** — every key finding **must** carry an inline `[Cn]` reference;
  findings without a citation are dropped.
- **LLM narration / numeric guard** — the LLM turns the reasoning graph into prose
  (`supporting_analysis`); a safety check (`verify_prose`) rejects any unsourced number, so
  the narrative can't drift from the cited facts. Falls back to a deterministic template if
  the LLM is unavailable.

**Output — `Response`:**
```
Response:
  direct_answer        "Lucky Cement's gross profit for 2025 was 13,967,091 (Rs thousand)."
  key_findings         ["… [C1]", "… [C2]"]          # each MUST carry a citation
  supporting_analysis  "<prose>"                       # LLM or template
  calculations         [ CalcResult ]
  evidence_used        [ claim strings ]               # audit trail
  citations            [ Citation ]
  conflicts            [ Conflict ]                     # exposed contradictions
  withheld             [ … ]                            # numbers dropped (too little evidence)
  confidence:
      band         High | Medium | Low
      score        0.0–1.0
      reasons[]  caps_applied[]  limited_by
      components [ {name, value, rationale} ]
  prose_source         deterministic | llm
  coverage { degraded, partial_coverage, dropped_insights, withheld, admission{role:count} }
```

---

## Layer 8 — Trace & Answer   ·   *Rule-Based*

**What happens:** Persist the full reasoning trail (for audit/replay), return the answer to
the client, and echo the resolved frame so the next follow-up question has context.

**Terms:**
- **TraceRecord** — a JSON record of the whole reasoning chain for this question, written to
  `fie_trace_dir` (one file per `trace_id`). Best-effort: a trace write never breaks the answer.
- **Frame echo** — the `QueryFrame` is returned with the answer so the client can send it back
  with the next question (this is what makes "and 2024?" work).

**Outputs:**
```
TraceRecord (sidecar  <trace_id>.json):
  trace_id, query, audience, company, frame, plan, evidence, response

API answer (returned to the UI):
  direct_answer, key_findings[], supporting_analysis, calculations[],
  citations[], confidence{band,score,…}, conflicts[], coverage{…},
  frame            # echoed for the next follow-up
```

---

### Cross-cutting (apply across all layers)
- **Conversation memory** — chat history powers follow-up resolution (Layer 2) and is echoed
  back (Layer 8).
- **Citation enforcement** — no finding ships without a `[Cn]`; no prose number without a source.
- **Contract checks (`bootcheck`)** — formula / authority / taxonomy / citation invariants are
  asserted at startup, so a mis-wired rule fails loudly instead of serving a wrong answer.
- **Degradation handling** — an external API failing never crashes a query; it caches/degrades
  and the confidence band is capped accordingly.
- **Reasoning trace** — every answer is fully reconstructable from its `trace.json`.

### One-line summary
`.xlsx + question → FactStore → QueryFrame → SourcePlan → EvidenceItem[] → Calcs+ctx → Roled evidence + Conflicts → Response → Answer + trace.json`
