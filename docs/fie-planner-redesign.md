# FIE Planner Redesign — Structured, Source-Scoped Plan

Status: **design / not yet wired**
Owner: FIE
Last updated: 2026-06-12

---

## 1. Why we are changing the planner

The current planner is a single-shot LLM call that takes the question + the
whole catalog and returns a free-form `needs[]` list. It keeps failing on
follow-ups and off-workbook subjects. The canonical failure:

```
turn 1   user: "list competitors"
         asst: "...13 companies: AGTL, ATLH, ... Indus Motors, ..."
turn 2   user: "gp margin of each"
         plan: [{kind:'formula', formula:'gross_margin', year:null}]   ← WRONG
         answer: "Gross profit margin for Millat Tractors Limited is ..."  ← workbook company, not the 13
```

### Root causes (confirmed from traces, not theory)

1. **The prompt is an 11.2 KB run-on blob** with ~15 competing directives. The
   ordered-reasoning / eligibility rules are buried mid-paragraph and the model
   does not follow them reliably.
2. **Our own worked example teaches the failure.** The prompt literally contains
   `'gp margin 2022' -> [{kind:'formula',formula:'gross_margin',year:2022}]`, so
   "gp margin of each" pattern-matches straight to the workbook formula.
3. **The payload is 94 % noise.** Of ~31.7 KB sent, the conversation (the part
   that actually disambiguates the follow-up) is ~633 chars — **2 %**. Tool text
   alone is ~18.8 KB.
4. **Eligibility was prose, and prose loses.** "If the subject is a different
   company, use tools" is one clause competing against a crisp example. The model
   stops at the first plausible path (premature source selection).

### The core idea of the redesign

Make the plan **structured and scoped by source**, so the *shape of the output*
enforces the rules we kept writing in prose:

- `sheets` / `metrics` / `areas` exist **only for the one workbook company**.
- A competitor (e.g. Indus Motors) has **no** sheets and **no** canonical metric
  ids — so the planner literally **cannot express** "gp margin of Indus" as a
  workbook need. The only shape that fits an off-workbook company is a **tool**
  need.

That turns the workbook-vs-tools eligibility gate from a paragraph the model
ignores into a **structural constraint** it cannot violate.

---

## 2. How extraction actually works (the constraint we must respect)

The only workbook value extractor is `FinancialFactStore.lookup()`
(`backend/app/engines/fie/store.py:208`):

```python
sel = df[(df["metric"] == metric) & (df["year"] == year)
       & (df["level"] == level) & (df["period_type"] == period_type)]
```

So a workbook value is keyed by **`metric` + `year` + `level`** (+ historical/
forecast). Findata columns
(`store.py:36`): `company, statement, level, sheet, cell, label, section, metric,
note_ref, year, period_type, value`.

Key facts that shape the design:

- **`metric` is a canonical ontology id** (`revenue`, `gross_profit`, …). It is
  the extraction key.
- **`sheet`/`cell` are for citation only** — `lookup()` does **not** filter by
  sheet. But the sheet name deterministically encodes **`level`** via
  `classify_sheet()` (`"P&L"`/`"Balance Sheet"` → headline; `"PL1 - Revenue"`,
  `"BS1 - …"` → detail). So we recover `level` from the sheet name in code; the
  planner never needs an explicit level field.
- **Detail sheets are mostly unaddressable below their total.** Example: the real
  `PL1 - Revenue` sheet has **210 rows** (`Tractors`, `Implements`, export lines,
  sales tax, …) but **only 1** maps to a canonical id (`revenue`, the total). The
  other 200 rows have `metric = None`, so they cannot be selected or fetched.
  ⇒ Listing a detail sheet in the menu mostly just re-states the headline total.

### Other sources

| source | extractor | filters |
|---|---|---|
| insights | `store.insights(area, year)` (`store.py:246`) | area, year |
| validation | `store.data_quality_flags(metric, years)` | metric, years |
| edit_history | `store.history` (list of dicts) | sheet, … |
| source ledger | `store.source_ledger` (DataFrame) | whole-sheet |
| forecast | forecast primitive | metric, year, growth |
| tools / news / web | tool dispatch | tool args / query |

Insights / ledgers / history are **whole-sheet primitives** — they are *not*
extracted column-by-column. So in the menu, their `columns` are only a **routing
hint** (so the planner knows what each can answer); they are not consumed by
extraction.

---

## 3. New planner INPUT — `describe_workbook()`

Replace the current flat metric dumps + mixed years with a scoped menu.

```jsonc
{
  "company": "Millat Tractors Limited",
  "ticker":  "MTL",
  "sector":  "AUTOMOBILE ASSEMBLER",

  // split — forecast years no longer masquerade as "data present"
  "years": { "historical": [2021, 2022, 2023, 2024, 2025], "forecast": [] },

  // flat { sheet : [canonical metric ids] }. level is derived from the sheet
  // name in code (classify_sheet), so no headline/detail grouping is needed.
  "sheets": {
    "P&L":            ["revenue", "cost_of_sales", "gross_profit", "operating_profit",
                       "profit_before_tax", "pat", "finance_cost", "taxation", "levy",
                       "administrative_expenses", "distribution_marketing_expenses",
                       "other_operating_expenses", "total_operating_expenses", "other_income",
                       "comprehensive_income", "other_comprehensive_income",
                       "defined_benefit_actuarial_gain_loss"],
    "Balance Sheet":  ["total_assets", "non_current_assets", "current_assets", "total_equity",
                       "total_equity_and_liabilities", "non_current_liabilities",
                       "current_liabilities", "property_plant_equipment", "intangible_assets",
                       "investment_property", "right_of_use_assets", "long_term_investments",
                       "long_term_deposits", "loans_and_advances", "stock_in_trade",
                       "trade_debts", "cash_and_bank", "stores_spares_loose_tools",
                       "deposits_prepayments_other_receivables", "paid_up_capital", "reserves",
                       "short_term_borrowings", "lease_liabilities", "contract_liabilities",
                       "creditors_accrued_other_liabilities", "unclaimed_dividend",
                       "dividends_paid", "defined_benefit_expense"]
    // DECIDED (D1): detail sheets (PL1.., BS1..) are OMITTED — they only carry
    // their total's canonical id, which already appears in the headline sheet.
    // So `sheets` is headline statements only: "P&L" and "Balance Sheet".
  },

  "formulas": [ /* registered ratio ids — id, expression, unit, description */ ],

  "qualitative_insights": {
    "columns": ["area", "takeaway", "year", "source_report_year", "source_section",
                "page", "confidence"],
    "areas":   [ /* populated at load from the Insights sheet */ ]
  },
  "edit_history":      { "columns": ["timestamp", "sheet", "cell", "old", "new", "saved", "event"] },
  "source_ledger":     { "columns": ["Sheet", "Cell", "Template label", "Matched label", "Year",
                                     "Value", "Report year", "Report file", "Page", "Table id",
                                     "Confidence", "Note"] },
  "validation_ledger": { "columns": ["Status", "Sheet", "Cell/Label", "Metric", "Year", "Value",
                                     "Face truth", "Source", "Note"] },

  "tools": [ /* unchanged: name, description, inputs, output fields */ ]
}
```

All of `sheets` / `formulas` / `areas` are buildable deterministically at load
from `findata` + parsers — no fetch-layer change.

---

## 4. New planner OUTPUT — structured, source-scoped

The planner returns ONE object. Each source is its own key; only the keys the
question needs are populated. **`needs[]` (free-form kinds) is removed.**

```jsonc
{
  "interpretation": "<one line: what the user is actually asking, with references resolved>",

  // ── workbook financial data (THE workbook company only) ──────────────
  // list of {sheet, metrics}; `years` is shared across all blocks.
  "financial": [
    { "sheet": "P&L",           "metrics": ["revenue", "gross_profit"] },
    { "sheet": "Balance Sheet", "metrics": ["total_assets"] }
  ],
  "years": [2023, 2024],

  // ── registered ratios (workbook company) ─────────────────────────────
  "formulas": ["gross_margin", "net_margin"],     // ids copied verbatim; uses `years`

  // ── ad-hoc arithmetic over workbook metric ids ───────────────────────
  "compute": [
    { "expression": "operating_profit/(total_assets-current_liabilities)",
      "label": "ROIC", "years": [2024] }
  ],

  // ── qualitative ──────────────────────────────────────────────────────
  "insights": { "areas": ["risks", "outlook"], "years": [2025] },

  // ── data audit / consistency ─────────────────────────────────────────
  "validation": { "metrics": ["total_assets"], "years": [2024] },

  // ── user's own edits ─────────────────────────────────────────────────
  "edit_history": { "sheets": ["P&L"] },          // or {} for all

  // ── projection ───────────────────────────────────────────────────────
  "forecast": [ { "metric": "revenue", "year": 2026, "growth": 0.10 } ],

  // ── ANYTHING about another company / sector / PSX live data ──────────
  // the ONLY shape an off-workbook subject can take.
  "tools": [
    { "tool": "getCompanyOverview", "args": { "company": "INDU" } },
    { "tool": "getCompanyOverview", "args": { "company": "ATLH" } }
  ],

  // ── open web / news fallback ─────────────────────────────────────────
  "news": [ { "query": "Millat Tractors dividend 2025" } ],
  "web":  [ { "query": "Pakistan tractor industry outlook 2025" } ],

  // ── only when genuinely ambiguous; mutually exclusive with the above ─
  "clarification": null,

  "hints": { "company": null, "sector": null, "years": [], "keywords": [] }
}
```

### Extraction per key (all map to existing code)

```python
# financial
for block in plan.get("financial", []):
    level = classify_sheet(block["sheet"])
    for metric in block["metrics"]:
        for year in plan["years"]:
            store.lookup(metric, year, level)

# formulas      -> calc registry, per id × plan["years"]
# compute       -> calc_registry.safe_eval(expression) per years
# insights      -> store.insights(area, year) for area×year
# validation    -> store.data_quality_flags(metric, plan_years)
# edit_history  -> filter store.history by sheets
# forecast      -> forecast primitive(metric, year, growth)
# tools         -> dispatch each {tool, args}
# news / web    -> escalation path
```

---

## 5. Why this fixes the canonical bug

`"gp margin of each"` after listing 13 competitors:

1. **RESOLVE** — `each` = the 13 named in the prior answer (still in `recent`).
2. **SHAPE FORCES THE SOURCE** — those companies are not the workbook company, so
   they have no `sheets`/`metrics`. The planner **cannot** put them under
   `financial` or `formulas`. The only legal shape is `tools[]`.
3. **FAN-OUT** — one `{tool:'getCompanyOverview', args:{company:'…'}}` per
   company (gross margin is a `getCompanyOverview` output field).

The "collapse to Millat's `gross_margin` formula" path is now **unrepresentable
in the schema**, not merely discouraged in prose.

---

## 6. Open decisions

- **D1 — detail sheets in the menu? → DECIDED: OMIT.** Detail sheets only expose
  their total's canonical id (PL1 → `revenue`), which duplicates the headline sheet.
  `sheets` is headline statements only (`P&L`, `Balance Sheet`); all `financial`
  needs are therefore `level="headline"`. The label-level breakdown (`Tractors`,
  export sales, sales tax …) stays out of LLM reach until a later label-based path.
- **D2 — shared `years` vs per-block years.** Current design: one `years` shared
  across all `financial` blocks (cross-producted). Accept the minor over-fetch?
  *Leaning yes (simpler planner).*
- **D3 — `recent` budget.** Keep the real Q&A transcript (verbatim, last ~6 turns,
  1000-char answer cap). Trim tool text so `recent` is no longer 2 % of payload.
- **D4 — deterministic guards (code, not prompt).** Even with the structural
  schema: (a) reject a `tools[]` entry whose `company` arg is a placeholder /
  unresolved phrase; (b) an empty plan is valid **only** if `clarification` is set,
  else it's a planning failure (do **not** silently fall back to the workbook
  company).

---

## 7. Build order (once §6 is signed off)

1. `describe_workbook()` → emit the new INPUT (§3). Show output on real workbook.
2. New `PLAN_SCHEMA` + a short, sectioned `PLAN_SYS` (§4). Delete the
   `gp margin → gross_margin` example.
3. Controller: replace `needs[]` dispatch with per-key extraction (§4) + guards
   (§6 D4).
4. Trim payload / keep verbatim `recent` (§6 D3).
5. Re-run the failing sequences live: "list competitors → gp margin of each",
   "systems limited gp margin → and in 2024", "revenue vs peers", "sector margins".

---

## 8. Formula menu — finalized (`id · description · unit`)

Decisions locked:
- **Gate dynamically** — send only formulas whose inputs exist in the *loaded*
  workbook (per-workbook, not Millat-specific).
- **Drop `expression`** from the menu — the engine computes from the registry;
  the planner selects by `id` + `description`.
- **Descriptions are now self-sufficient and unique** (the old menu described
  `gross_margin`/`operating_margin`/`net_margin` all as "profitability").

These descriptions replace the registry `description` field. Menu line form:
`gross_margin — Gross profit as a percentage of revenue. (%)`

| id | description | unit |
|---|---|---|
| revenue_growth | Year-over-year percentage change in revenue. | % |
| earnings_growth | Year-over-year percentage change in net profit (PAT). | % |
| gross_margin | Gross profit as a percentage of revenue (revenue after direct/production costs). | % |
| operating_margin | Operating profit (EBIT) as a percentage of revenue. | % |
| net_margin | Net profit (PAT) as a percentage of revenue. | % |
| pretax_margin | Profit before tax as a percentage of revenue. | % |
| cogs_ratio | Cost of goods sold as a percentage of revenue. | % |
| opex_ratio | Operating expenses as a percentage of revenue. | % |
| effective_tax_rate | Income tax expense as a percentage of profit before tax. | % |
| roe | Net profit as a percentage of average shareholders' equity (return on equity). | % |
| roa | Net profit as a percentage of average total assets (return on assets). | % |
| return_on_capital_employed | Operating profit (EBIT) as a percentage of capital employed (ROCE). | % |
| equity_ratio | Share of total assets financed by equity. | % |
| current_ratio | Current assets ÷ current liabilities (short-term liquidity). | x |
| quick_ratio | Liquid current assets excluding inventory ÷ current liabilities. | x |
| cash_ratio | Cash and equivalents ÷ current liabilities. | x |
| debt_to_equity | Total debt relative to total equity (financial leverage). | x |
| debt_to_assets | Share of total assets financed by debt. | x |
| long_term_debt_ratio | Long-term debt as a share of total assets. | x |
| equity_multiplier | Total assets ÷ total equity (leverage). | x |
| interest_coverage | Operating profit (EBIT) relative to interest expense. | x |
| asset_turnover | Revenue generated per unit of average total assets. | x |
| fixed_asset_turnover | Revenue generated per unit of net fixed assets. | x |
| inventory_turnover | Times inventory is sold and replaced (COGS ÷ inventory). | x |
| receivables_turnover | Times receivables are collected (revenue ÷ receivables). | x |
| days_sales_outstanding | Average days to collect receivables (DSO). | days |
| days_inventory_outstanding | Average days inventory is held before sale (DIO). | days |
| working_capital | Current assets minus current liabilities. | currency |
| capital_employed | Total assets minus current liabilities (long-term capital in use). | currency |
| net_debt | Total debt minus cash and equivalents. | currency |
| ebitda | Earnings before interest, tax, depreciation and amortization. | currency |
| ebitda_margin | EBITDA as a percentage of revenue. | % |
| debt_to_ebitda | Total debt relative to EBITDA. | x |
| free_cash_flow | Operating cash flow minus capital expenditure. | currency |
| free_cash_flow_margin | Free cash flow as a percentage of revenue. | % |
| operating_cash_flow_margin | Operating cash flow as a percentage of revenue. | % |
| operating_cash_flow_ratio | Operating cash flow relative to current liabilities. | x |
| cash_flow_to_debt | Operating cash flow relative to total debt. | x |
| capex_to_sales | Capital expenditure as a percentage of revenue. | % |
| payables_turnover | Speed of paying suppliers (COGS ÷ payables). | x |
| days_payable_outstanding | Average days taken to pay suppliers (DPO). | days |
| cash_conversion_cycle | Days to convert inventory/receivables back to cash (DSO + DIO − DPO). | days |
| book_value_per_share | Common equity per outstanding share. | currency |
| eps_computed | Net profit per weighted-average outstanding share (EPS). | currency |
| dividend_payout_ratio | Share of net profit paid out as dividends. | % |
| retention_ratio | Share of net profit retained (1 − payout ratio). | % |
| forecast_error | Percentage deviation of actual from forecast. | % |

---

## 9. Tool menu — schema + examples (all 34)

The current tool entry is `{name, description, inputs:[{name,type,desc}], outputs:"<prose>"}`.
Two problems: `outputs` is prose the planner must parse, and there are **no examples**,
so the planner emitted placeholders like
`getCompanyOverview("each company from the prior comparison set")`.

### New tool-entry schema

```jsonc
{
  "name": "getCompanyOverview",
  "desc": "Full PSX profile for ANY listed company: live quote (price, P/E TTM, day range, 52-wk), equity (market cap, shares, free float), profile (business, key people, auditor, fiscal year-end), per-year financials (sales, PAT, EPS) and ratios. Go-to for 'tell me about X', valuation, margins, EPS, market cap.",
  "inputs": [
    { "name": "company", "type": "string", "required": true, "desc": "company name OR PSX ticker (auto-detected)" }
  ],
  "outputs": ["price","change_pct","pe_ttm","day_range","week52_range","market_cap","shares",
              "free_float","fiscal_year_end","sales","pat","eps","eps_growth",
              "gross_margin_pct","net_margin_pct","peg"],
  "examples": ["getCompanyOverview(\"Millat Tractors Limited\")", "getCompanyOverview(\"MTL\")"]
}
```

Changes vs now:
- `description` → `desc`; `inputs` stays an **array** (some tools take >1 input) and gains `required`.
- `outputs` becomes a **flat list of field names** (planner greps `gross_margin_pct` to answer "gp margin") — converted from each tool's existing prose at build time.
- every tool gets **`examples`** showing the arg shape (name AND ticker; with AND without optionals; sector form).

### Examples for all 34 tools

Optional inputs marked `?`. Examples deliberately mix **company name vs ticker**,
**with vs without optional args**, and **sector** forms.

| tool | inputs | examples |
|---|---|---|
| getCompanyOverview | company | `getCompanyOverview("Millat Tractors Limited")` · `getCompanyOverview("MTL")` |
| getCompanyScreener | company | `getCompanyScreener("Lucky Cement")` · `getCompanyScreener("LUCK")` |
| getCompanySnapshot | company | `getCompanySnapshot("Millat Tractors Limited")` · `getCompanySnapshot("MTL")` |
| getCompanyMarketWatch | company | `getCompanyMarketWatch("Indus Motor Company Limited")` · `getCompanyMarketWatch("INDU")` |
| getCompanyFutures | company | `getCompanyFutures("Lucky Cement")` · `getCompanyFutures("LUCK")` |
| getCompanyCashSettledFutures | company | `getCompanyCashSettledFutures("Millat Tractors Limited")` · `getCompanyCashSettledFutures("MTL")` |
| getCompanySymbol | company | `getCompanySymbol("Millat Tractors Limited")` · `getCompanySymbol("Lucky Cement")` |
| getCompanySector | company | `getCompanySector("MTL")` · `getCompanySector("Lucky Cement")` |
| getCompanyCompetitors | company | `getCompanyCompetitors("Millat Tractors Limited")` · `getCompanyCompetitors("MTL")` |
| getCompanyPayouts | company, limit? | `getCompanyPayouts("MTL")` · `getCompanyPayouts("Lucky Cement", 20)` |
| getCompanyAnnouncements | company, limit? | `getCompanyAnnouncements("INDU")` · `getCompanyAnnouncements("Indus Motor Company Limited", 15)` |
| getCompanySECPNotices | company, limit? | `getCompanySECPNotices("MTL")` · `getCompanySECPNotices("Lucky Cement", 15)` |
| getCompanyVsSectorFundamentals | company, year | `getCompanyVsSectorFundamentals("MTL", 2025)` · `getCompanyVsSectorFundamentals("Lucky Cement", 2024)` |
| getCompanyAnalysisReport | company, year | `getCompanyAnalysisReport("MTL", 2025)` · `getCompanyAnalysisReport("Indus Motor Company Limited", 2024)` |
| getCompanyPeerComparison | company, metric?, limit? | `getCompanyPeerComparison("MTL")` · `getCompanyPeerComparison("Lucky Cement", "market_cap", 10)` |
| getSectorCompanies | sector | `getSectorCompanies("CEMENT")` · `getSectorCompanies("AUTOMOBILE ASSEMBLER")` |
| getSectorMarketWatch | sector | `getSectorMarketWatch("CEMENT")` · `getSectorMarketWatch("AUTOMOBILE ASSEMBLER")` |
| getSectorTurnover | (none) | `getSectorTurnover()` |
| getSectorMarketSummary | sector, limit? | `getSectorMarketSummary("CEMENT")` · `getSectorMarketSummary("AUTOMOBILE ASSEMBLER", 200)` |
| getSectorScreener | sector, limit? | `getSectorScreener("CEMENT")` · `getSectorScreener("AUTOMOBILE ASSEMBLER", 60)` |
| getSectorAnalysisReport | sector, year?, limit? | `getSectorAnalysisReport("AUTOMOBILE ASSEMBLER")` · `getSectorAnalysisReport("CEMENT", 2025, 60)` |
| getSectorAnnouncements | sector, max_companies?, per_company? | `getSectorAnnouncements("CEMENT")` · `getSectorAnnouncements("AUTOMOBILE ASSEMBLER", 8, 5)` |
| getSectorSECPNotices | sector, max_companies?, per_company? | `getSectorSECPNotices("CEMENT")` · `getSectorSECPNotices("AUTOMOBILE ASSEMBLER", 8, 5)` |
| screenStocks | metric?, sector?, order?, limit? | `screenStocks()` · `screenStocks("pe_ratio_ttm", "CEMENT", "asc", 10)` |
| getMarketWatch | limit? | `getMarketWatch()` · `getMarketWatch(20)` |
| getMarketSummary | (none) | `getMarketSummary()` |
| getTopActiveStocks | n? | `getTopActiveStocks()` · `getTopActiveStocks(5)` |
| getTopAdvancers | n? | `getTopAdvancers()` · `getTopAdvancers(10)` |
| getTopDecliners | n? | `getTopDecliners()` · `getTopDecliners(10)` |
| getFutures | limit? | `getFutures()` · `getFutures(10)` |
| getCashSettledFutures | limit? | `getCashSettledFutures()` · `getCashSettledFutures(10)` |
| getDebtMarketWatch | limit? | `getDebtMarketWatch()` · `getDebtMarketWatch(50)` |
| getTopActiveDebtSecurities | n? | `getTopActiveDebtSecurities()` · `getTopActiveDebtSecurities(5)` |
| getTopDebtAdvancers | n? | `getTopDebtAdvancers()` · `getTopDebtAdvancers(5)` |

### Fan-out (taught once in PLAN_SYS, not per tool)

Per-tool examples teach **arg shape**. Multi-company **fan-out** is taught once in
the prompt, so the planner emits one call per resolved company:

```
After a turn listing competitors AGTL, ATLH, INDU →
"gp margin of each":
  getCompanyOverview("AGTL")
  getCompanyOverview("ATLH")
  getCompanyOverview("INDU")
NEVER getCompanyOverview("each company from the comparison set").
```

---

## 10. Recent conversation (chat history) input

This is the block whose burial caused the canonical bug: the 13 competitors WERE
sent, but at the **end** of a 43 KB wall after 31 KB of catalogs, so the planner
reported "no comparison set is present" (§5).

### Shape — uniform `{timestamp, role, content}`

Replace the current asymmetric `{role:'user', question}` / `{role:'assistant', answer}`
with one uniform message shape:

```jsonc
"recent_messages": [
  { "timestamp": "2026-06-12T09:15:23Z", "role": "user",      "content": "Should we move to model implementation?" },
  { "timestamp": "2026-06-12T09:16:10Z", "role": "assistant", "content": "Yes, start with parsing models..." }
]
```

### Rules

- **Verbatim** — real user text and the assistant's **actual answer**. No `resolved`
  projection, no distillation (that lossy projection was the original source of
  false-inheritance bugs).
- **Window** — last ~6 turns (≈12 messages), oldest→newest.
- **`content` cap** — assistant messages capped (~1000 chars) so a long list answer
  (e.g. the 13 companies) survives intact but a giant table doesn't blow the budget.
  User messages are short; no cap needed.
- **`role`** — `"user"` | `"assistant"`, same key for both (drop the question/answer split).
- **`timestamp`** — ISO-8601; lets the planner resolve relative time refs
  ("since then", "last week") and confirms ordering. Cheap; keep it.
- **NO citations / frames / metadata** — the planner does not need them; they were
  noise. Only `timestamp`, `role`, `content`.

### Placement — FIRST, not last

`recent_messages` must appear **near the top** of the planner payload, before the
`sheets` / `formulas` / `tools` catalogs — never buried after them. Burying it under
31 KB of menus is exactly what made the model miss the conversation. Order:

```
question → recent_messages → workbook(company/sector/years) → sheets → formulas → tools
```

### Maps to code

`controller._recent_context()` already sends verbatim Q&A; this change is:
(1) rename to `recent_messages`, (2) emit uniform `{timestamp, role, content}`,
(3) move the block ahead of the catalogs in the payload, (4) update `PLAN_SYS`'s
`recent` references to `recent_messages` / `content`.

---

## 11. Qualitative + ledger/history inputs — finalized

### `qualitative_insights` — `areas`, `years`, `schema`

The planner needs `areas` (to scope `insights:{areas, years}`) and the `years`
insights actually exist for; `schema` documents the record fields.

```jsonc
"qualitative_insights": {
  "areas":  ["risks", "strategy", "outlook", "governance", "demand", "competition"],  // from the Insights sheet
  "years":  [2023, 2024, 2025],                                                       // years with insight rows
  "schema": ["area", "takeaway", "year", "source_report_year", "source_section", "page", "confidence"]
}
```

### `edit_history` / `source_ledger` / `validation_ledger` — `capability`, `schema`

Extraction for these is **whole-sheet** (not column-keyed), so each carries a
one-line **`capability`** (so the planner can route to it) plus its **`schema`**
(field names). No row data in the menu.

```jsonc
"edit_history": {
  "capability": "the user's own edits to this workbook — what changed, in which sheet/cell, when, saved vs unsaved.",
  "schema": ["timestamp", "sheet", "cell", "old", "new", "saved", "event"]
},
"source_ledger": {
  "capability": "provenance of each figure — which source report / page / table a value was taken from.",
  "schema": ["Sheet", "Cell", "Template label", "Matched label", "Year", "Value",
             "Report year", "Report file", "Page", "Table id", "Confidence", "Note"]
},
"validation_ledger": {
  "capability": "data-quality audit — which metric/year cells are flagged, their status, vs face/source truth.",
  "schema": ["Status", "Sheet", "Cell/Label", "Metric", "Year", "Value", "Face truth", "Source", "Note"]
}
```

These map to the existing primitives unchanged: `{kind:'insights'}` →
`store.insights(area, year)`; `edit_history` → `store.history`; `validation` →
`store.validation_ledger` / `data_quality_flags`; provenance → `store.source_ledger`.

---

## 12. `PLAN_SCHEMA` — structured plan output

Replaces the old free-form `needs[]`. One object; the planner populates **only the
keys the question needs** (omit the rest). Every key is source-scoped (§4–§5), so an
off-workbook subject can only be expressed as `tools`/`web`, never as workbook
`financial`/`formulas`.

```jsonc
{
  "type": "object",
  "properties": {
    "interpretation": { "type": "string",
      "description": "one line: what the user is actually asking, with all references resolved" },

    // ── workbook financial data (THE workbook company only) ──────────────
    "financial": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "sheet":   { "type": "string",
                       "description": "a key from input `sheets` — headline only: 'P&L' or 'Balance Sheet'" },
          "metrics": { "type": "array", "items": { "type": "string" },
                       "description": "canonical metric ids from that sheet's list" }
        },
        "required": ["sheet", "metrics"]
      }
    },
    "years": { "type": "array", "items": { "type": "integer" },
               "description": "fiscal years shared across all `financial`/`formulas` blocks" },

    // ── registered ratios (workbook company); uses `years` ───────────────
    "formulas": { "type": "array", "items": { "type": "string" },
                  "description": "formula ids copied verbatim from input `formulas`" },

    // ── ad-hoc arithmetic over workbook metric ids ───────────────────────
    "compute": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "expression": { "type": "string",
                          "description": "arithmetic over canonical metric ids ONLY; no literal numbers" },
          "label":      { "type": "string" },
          "years":      { "type": "array", "items": { "type": "integer" } }
        },
        "required": ["expression", "label", "years"]
      }
    },

    // ── qualitative ──────────────────────────────────────────────────────
    "insights": {
      "type": "object",
      "properties": {
        "areas": { "type": "array", "items": { "type": "string" },
                   "description": "areas from input qualitative_insights.areas" },
        "years": { "type": "array", "items": { "type": "integer" } }
      }
    },

    // ── data audit / consistency ─────────────────────────────────────────
    "validation": {
      "type": "object",
      "properties": {
        "metrics": { "type": "array", "items": { "type": "string" } },
        "years":   { "type": "array", "items": { "type": "integer" } }
      }
    },

    // ── user's own edits ─────────────────────────────────────────────────
    "edit_history": {
      "type": "object",
      "properties": {
        "sheets": { "type": "array", "items": { "type": "string" },
                    "description": "optional sheet filter; omit/[] for all edits" }
      }
    },

    // ── projection ───────────────────────────────────────────────────────
    "forecast": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "metric": { "type": "string" },
          "year":   { "type": "integer" },
          "growth": { "type": ["number", "null"],
                      "description": "decimal fraction if the user stated a rate (10% -> 0.10), else null" }
        },
        "required": ["metric", "year"]
      }
    },

    // ── ANY other company / sector / PSX live data (the ONLY off-workbook shape) ─
    "tools": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "tool": { "type": "string", "description": "a name from input `tools`" },
          "args": { "type": "object",
                    "description": "literal arg values (company name/ticker, sector, ints); NEVER a placeholder phrase" }
        },
        "required": ["tool", "args"]
      }
    },

    // ── open web / news fallback ─────────────────────────────────────────
    "news": { "type": "array",
              "items": { "type": "object",
                         "properties": { "query": { "type": "string" } }, "required": ["query"] } },
    "web":  { "type": "array",
              "items": { "type": "object",
                         "properties": { "query": { "type": "string" } }, "required": ["query"] } },

    // ── only when genuinely ambiguous; then ALL source arrays must be empty ─
    "clarification": { "type": ["string", "null"] },

    "hints": {
      "type": "object",
      "properties": {
        "company":  { "type": ["string", "null"] },
        "sector":   { "type": ["string", "null"] },
        "years":    { "type": "array", "items": { "type": "integer" } },
        "keywords": { "type": "array", "items": { "type": "string" } }
      }
    }
  },
  "required": ["interpretation", "hints"]
}
```

### Validity rules (enforced in code — D4)

- `financial[].sheet` must be a key from input `sheets`; `financial[].metrics` must
  be ids listed under that sheet. `formulas` ids and `tools` names must exist in the
  inputs. → reject/repair otherwise.
- `tools[].args` values must be literal (resolved company name/ticker, sector, int).
  A placeholder/unresolved phrase (`"each company from the comparison set"`) is
  rejected. → D4(a).
- An empty plan (every source array empty / absent) is valid **only** if
  `clarification` is set; otherwise it is a planning failure — do **not** silently
  fall back to the workbook company. → D4(b).

---

## 13. `PLAN_SYS` slimming — what the prompt no longer needs to say

The current `PLAN_SYS` is **11,262 chars** because it teaches in prose what the new
input structure + `PLAN_SCHEMA` + code guards now enforce mechanically. Teardown of
the existing prompt and the verdict for each block:

| # | section | ~chars | verdict | why |
|---|---|---|---|---|
| 1 | Role + restrictions | ~450 | keep | short, still needed |
| 2 | INPUTS description | ~450 | shrink | rewrite to new inputs (one line) |
| 3 | OUTPUT (`needs is a LIST…`) | ~450 | replace | `PLAN_SCHEMA` (§12) defines the shape |
| 4 | PLANNING ORDER (5 steps) | ~1,150 | keep / condense | the core (STEP 1→2→3) |
| 5 | **`EACH need is one of {kind:'metric'…tool…compute…}`** | **~2,600** | **remove** | `PLAN_SCHEMA` defines every key's shape; no need to spell out 11 JSON shapes + the long compute paragraph |
| 6 | HOW TO CHOOSE (with tool-field prose) | ~1,150 | shrink hard | tools now carry a structured `outputs` list — model reads fields directly |
| 7 | COMPOUND QUESTIONS | ~450 | shrink | → one clause |
| 8 | FAN-OUT over a set | ~750 | keep / condense | important; + one example |
| 9 | NESTED / COMPOSITE TOOLS | ~450 | shrink | → one clause |
| 10 | **OFF-WORKBOOK** ("never relabel this company's figures") | ~350 | **remove → 1 line** | now **structural** (§5): another company has no `financial`/`formulas` shape |
| 11 | DATA FACT (PSX no COGS; margin via getCompanyOverview) | ~300 | **remove** | encoded in tool `outputs` (sector tools list `sales/pat/pbt`; getCompanyOverview lists `gross_margin_pct`) |
| 12 | CONVERSATION (resolve follow-ups) | ~750 | keep / condense | → STEP 1 |
| 13 | CLARIFY | ~700 | condense | → STEP 3 last-resort clause |
| 14 | EXECUTABLE OUTPUT | ~300 | **remove** | enforced by D4(b) code guard |
| 15 | ARGUMENT GROUNDING (no placeholder args) | ~450 | shrink | enforced by D4(a); keep one reminder line |
| 16 | ids verbatim + fill hints | ~350 | keep | one line each |
| 17 | **EXAMPLES** (incl. `gp margin → gross_margin`) | ~1,200 | **remove most** | per-tool examples live in the `tools` menu; **delete the gp-margin example** (taught the collapse); keep only fan-out |

### Deletable outright (~5 KB), because something stronger now enforces it

- 11-kind enumeration (#5) → `PLAN_SCHEMA`
- OFF-WORKBOOK enforcement (#10) → structural eligibility (§5)
- DATA FACT (#11) → structured tool `outputs` (§9)
- EXECUTABLE OUTPUT (#14) + most of ARGUMENT GROUNDING (#15) → D4 code guards
- EXAMPLES block (#17) → per-tool examples in the menu (minus the harmful one)

### What survives (~2 KB — the actual reasoning)

role + restrictions · ordered STEP 1→2→3 · fan-out + its one example · clarify as
last resort · "copy ids verbatim" · fill `hints`.

**Result: 11.2 KB → ~2 KB.** The cuts are not mere trimming — each removed block is
replaced by a stronger guarantee (schema shape, structured tool outputs, or a code
guard) instead of prose the model could ignore.

> NOTE: the full STEP 0–3 prompt wording is the *understanding* half only and is
> still being designed — it is deliberately NOT pasted here yet.
```
