# FIE Phase 0 — Foundation: Data Model & Ingestion

**Parent:** [`fie_implementation_plan.md`](fie_implementation_plan.md) · **Architecture:** [`financial_intelligence_engine.md`](financial_intelligence_engine.md)
**Layer:** L0 only (ingestion + typed model). **No reasoning, no LLM, no external APIs.**
**Why first:** the data model is the one true universal dependency — every later layer
consumes `FactRef` / `EvidenceItem` / `Citation` and the `FinancialFactStore`. This is
the only place where bottom-up is the correct order.

---

## 1. Objective & scope

Build the substrate that turns a delivered workbook into a typed, queryable,
provenance-carrying in-memory model.

**In scope**
- Core types: `FactRef`, `EvidenceItem`, `Citation` (pydantic; JSON/dict-serializable).
- `FinancialFactStore.from_workbook()` — parse every sheet into:
  - **findata → pandas DataFrame** (long format) — statements + detail.
  - **ledgers → pandas DataFrame** — `Source Ledger`, `Validation Ledger`.
  - **insights → JSON** (`list[dict]`) — `Insights` + `Insights Review`.
  - **manifest → dict** — sibling `*.manifest.json`.
- Lookup + provenance-resolution primitives (`lookup`, `detail`, `insights`, `cite`).
- A seed **metric ontology** (label → canonical metric id).

**Explicitly out of scope (later phases)**
- Any calculation/derivation (L4), conflict logic (L6), confidence (L7), prose (L8).
- The extraction validator / face-truth comparison — that is the dev harness in Phase 2.8.
- External sources.

---

## 2. Ground truth: the actual workbook schema

Verified against `millat_filled_fixed.xlsx` (parsing must be template-driven and is
validated against `lucky_filled_fixed.xlsx` too).

### 2.1 Sheet inventory & classification

| Class | Sheets | Detection rule |
|---|---|---|
| **Separators** (ignore) | `Input Sheets ------>>>>`, `P&L Breakdown-------->>>>`, `Balance Sheet Breakdown ----->>`, `Output -------->>>>>` | title contains `>>` / `---` and has no data grid |
| **Statement (headline)** | `P&L`, `Balance Sheet` | exact known names; band row + year row present |
| **Detail** | `PL1 – Revenue` … `PL7 – OCI`, `BS1 – Non-Current Assets` … `BS5 – Current Liabilities` | name matches `^(PL\|BS)\d` |
| **Insights** | `Insights`, `Insights Review` | header `Year, Source Report Year, Area, Takeaway, …` |
| **Source Ledger** | `Source Ledger` | header begins `Sheet, Cell, Template label, …` |
| **Validation Ledger** | `Validation Ledger` | header begins `Status, Sheet, Cell/Label, …` |
| **Free-text** (skip in P0) | `Mgmt info.`, `Qualtitative Data` | single-cell title only, no grid |

### 2.2 Statement sheets (`P&L`, `Balance Sheet`) — **wide layout**

```
A1: <Company>                       A2: <statement title>
A3: (Rupees in thousand)  C3: Historical ............ H3: Forecasted .........
A4: Particulars  B4: Notes  C4:2021 D4:2022 E4:2023 F4:2024 G4:2025 H4:2026 ... L4:2030
A6: Revenue ...   B6:32  C6:45,665,237 ... G6:52,108,997  (H..L empty = forecast slots)
A5/A10/...: SECTION HEADERS (all-caps, no values) → skip
```

- **Header row** = the row whose cells are mostly integers in 19xx–20xx → that row maps
  **column → fiscal year**. Do **not** hardcode (`P&L` header is row 4).
- **Band row** = the row above with `Historical` / `Forecasted` spans → maps
  **column → `period_type`**.
- Column A = metric label, column B = note ref, year columns = values.

### 2.3 Detail sheets (`PL*`, `BS*`) — **different offset**

```
A1: Note 32 – Revenue from Contracts with Customers
A2: (Rupees in thousand)  B2: Historical  G2: Forecasted
A3: Particulars  B3:2021 C3:2022 D3:2023 E3:2024 F3:2025 G3:2026 H3:2027
A4: LOCAL SALES (section)        A5:   Tractors  B5:42,610,262 ...
```

- **Header on row 3**, years **start at column B** (not C). Confirms per-sheet detection
  is mandatory.

### 2.4 `Source Ledger` (660 rows) — **provenance backbone, detail-only**

Columns: `Sheet · Cell · Template label · Matched label · Year · Value · Report year ·
Report file · Page · Table id · Confidence · Note`.

> **Critical:** every `Sheet` value is a **detail sheet** (`PL3 – Expenses`, `BS1 …`, …).
> **`P&L` and `Balance Sheet` never appear.** Headline figures therefore have **no direct
> provenance row** — citations must be resolved *through the detail sheet* (see §6).

### 2.5 `Validation Ledger` (55 rows) — **dev signal, loaded but not used at runtime**

Columns: `Status · Sheet · Cell/Label · Metric · Year · Value · Face truth · Source ·
Note`; `Status ∈ {WITHHELD, MISMATCH, NO_FACE_TRUTH}`. Loaded into a DataFrame for the
Phase 2.8 validator; **not** consulted on the answer path (§0.3 of the architecture).

### 2.6 `Insights` / `Insights Review` (104 / sparse rows)

Columns: `Year · Source Report Year · Area · Takeaway · Source Section · Page ·
Confidence`. Text-heavy → JSON records.

### 2.7 Manifest (`<workbook>.manifest.json`)

`company · production_ready · fully_reconciled · validation_failures · detail_incomplete
· headline_overrides · template_formulas_repaired`. One company per workbook.

---

## 3. Target in-memory model (what `from_workbook` produces)

```
FinancialFactStore
├── company: str                        # from manifest
├── unit: str = "Rupees in thousand"
├── years: list[int]                    # discovered, e.g. [2021..2030]
├── findata: pd.DataFrame   (long)      # statements + detail, one row per value cell
├── source_ledger: pd.DataFrame         # raw + indexed
├── validation_ledger: pd.DataFrame     # raw (dev-only)
├── insights: list[dict]                # JSON records, insight_id assigned
├── manifest: dict
└── ontology: MetricOntology            # label → canonical metric id
```

### 3.1 `findata` long schema (the heart of Phase 0)

| column | type | source | notes |
|---|---|---|---|
| `company` | str | manifest | |
| `statement` | str | sheet class | `pl` \| `bs` |
| `level` | str | sheet | `headline` (P&L/BS) \| `detail` (PL*/BS*) |
| `sheet` | str | sheet title | e.g. `P&L`, `PL1 – Revenue` |
| `cell` | str | A1 ref | e.g. `F6` — the value cell |
| `label` | str | col A | raw row label, trimmed |
| `section` | str\|None | nearest section header above | e.g. `OPERATING EXPENSES`, `LOCAL SALES` |
| `metric` | str\|None | ontology(label) | canonical id, e.g. `revenue`; None if unmapped |
| `note_ref` | str\|None | col B | e.g. `32` |
| `year` | int | header row | |
| `period_type` | str | band row | `historical` \| `forecasted` |
| `value` | float\|None | cell | sign preserved as stored (`cost_of_sales` negative) |

`headline` rows are the authoritative answer surface; `detail` rows are the citation
bridge (§6) and gap-fill source for later derivation.

### 3.2 Indices built at load

- `findata` indexed by `(company, level, metric, year)` and `(sheet, cell)`.
- `source_ledger` indexed by `(sheet, cell)` **and** `(sheet, year, matched_label_norm)`.
- `insights` carry a stable `insight_id` (`INS-{row}` within the source sheet).

---

## 4. Core types

JSON/dict-serializable pydantic v2 models. These are the contract every later layer
depends on, so they are frozen here.

```python
# models.py
from pydantic import BaseModel, Field
from typing import Literal, Optional

ValidationStatus = Literal["CLEAN", "MISMATCH", "WITHHELD", "NO_FACE_TRUTH"]
PeriodType       = Literal["historical", "forecasted"]

class FactRef(BaseModel):
    """Immutable identity of a single financial value + its provenance."""
    company: str
    metric: str                          # canonical id (may equal raw label if unmapped)
    label: str                           # raw workbook label
    year: int
    period_type: PeriodType = "historical"
    value: Optional[float]               # None when absent/withheld
    unit: str = "Rupees in thousand"
    statement: Literal["pl", "bs"]
    level: Literal["headline", "detail"]
    sheet: str
    cell: str
    # provenance (resolved via Source Ledger; see §6)
    source_ref: Optional[str] = None     # "<report_file>:p<page>:<table_id>"
    report_year: Optional[int] = None
    provenance_basis: Literal["direct", "via_detail", "none"] = "none"
    # dev-only annotation, never gates runtime
    validation_status: ValidationStatus = "CLEAN"

class Citation(BaseModel):
    ref_id: str                          # inline handle e.g. "C7"
    kind: Literal["financial", "insight", "external", "forecast"]
    display: str                         # "MTL Annual Report 2024-25, p108"
    locator: dict                        # {report_file, page, table_id, sheet, cell, year}
    confidence: Optional[float] = None   # Source Ledger confidence, if present
    retrieved_at: Optional[str] = None   # external only

class EvidenceItem(BaseModel):
    claim: str
    value: Optional[float] = None
    unit: Optional[str] = None
    kind: Literal["statement", "detail", "insight", "external", "calc"]
    fact_refs: list[FactRef] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    reliability: float = 1.0
    freshness: Optional[str] = None
    as_of: Optional[str] = None
```

> Design notes
> - `FactRef.provenance_basis` makes the headline→detail indirection (§6) explicit and
>   auditable: `direct` (a detail cell found in `Source Ledger`), `via_detail` (a headline
>   figure cited through its detail rows), or `none` (unciteable → must be withheld later).
> - `period_type` is first-class so Phase 4 forecast intents can read in-workbook forecasts.
> - `validation_status` is carried for the dev validator only; runtime ignores it (§0.3).

---

## 5. `FinancialFactStore` API (Phase 0 surface)

```python
# store.py
import pandas as pd

class FinancialFactStore:
    @classmethod
    def from_workbook(cls, path: str, *, manifest_path: str | None = None
                      ) -> "FinancialFactStore": ...

    # --- primary lookups ---
    def lookup(self, metric: str, year: int, *,
               level: Literal["headline", "detail"] = "headline",
               period_type: PeriodType = "historical") -> FactRef:
        """Authoritative value + resolved provenance. Raises KeyError if absent."""

    def detail(self, *, sheet: str | None = None, metric: str | None = None,
               year: int | None = None) -> pd.DataFrame:
        """Line-item rows (PL*/BS*) for gap-fill / citation bridging."""

    def insights(self, *, area: str | None = None, year: int | None = None,
                 min_confidence: float = 0.0, include_review: bool = True
                 ) -> list[dict]:
        """Filtered insight records (relevance ranking is Phase 2, not here)."""

    def cite(self, fact: FactRef) -> list[Citation]:
        """Resolve provenance for a FactRef into Citation objects (§6)."""

    # --- introspection ---
    @property
    def years(self) -> list[int]: ...
    @property
    def manifest(self) -> dict: ...
    def coverage(self) -> dict:
        """Counts: cells parsed, metrics mapped/unmapped, headline figures with/without
        resolvable provenance — surfaced for the smoke tests & later partial_coverage."""
```

Phase 0 implements lookup/retrieval/citation-resolution **only**. No formula, no scoring.

---

## 6. Provenance binding strategy (the central problem)

Because headline `P&L`/`Balance Sheet` cells are absent from the `Source Ledger`
(§2.4), `cite()` resolves in three tiers:

1. **Direct** — the `FactRef`'s own `(sheet, cell)` exists in `Source Ledger`
   (true for `detail` rows). Use that row → `source_ref`, `report_year`, `confidence`.
   Set `provenance_basis = "direct"`.
2. **Via detail** — for a `headline` metric, map it to its **contributing detail sheet**
   (ontology + the `PL#/BS#` → statement-line mapping) and pull the `Source Ledger`
   row(s) for `(detail_sheet, year, matched_label≈metric)`. Aggregate their
   `report_file/page` into one or more citations. Set `provenance_basis = "via_detail"`.
3. **None** — no detail rows resolve → `provenance_basis = "none"`, `source_ref = None`.
   Phase 0 records it in `coverage()`; later layers withhold such numbers (§7.2 arch).

**Report-year preference (restatement-aware, §0.3 arch):** when multiple `Source Ledger`
rows exist for the same `(metric, year)` with different `report_year`, Phase 0 keeps
**all** of them on the `FactRef` (as candidate citations) and tags the newest
`report_year` as primary. It does **not** resolve restatement conflicts — that is L6.

A small static map seeds tier 2 (extensible, data-not-code):

```python
# ontology.py  (seed)
STATEMENT_LINE_TO_DETAIL = {
    "revenue":         "PL1 - Revenue",
    "cost_of_sales":   "PL2 - Cost of Sales",
    "operating_expenses": "PL3 - Expenses",
    "other_income":    "PL4 - Other Income",
    "finance_cost":    "PL5 - Finance Cost",
    "levy":            "PL6 - Levy",
    "oci":             "PL7 - OCI",
    "non_current_assets":      "BS1 - Non-Current Assets",
    "current_assets":          "BS2 - Current Assets",
    "share_capital_reserves":  "BS3 - Share Capital & Reserves",
    "non_current_liabilities": "BS4 - Non-Current Liabilities",
    "current_liabilities":     "BS5 - Current Liabilities",
}
```

---

## 7. Metric ontology (seed)

Decouples report wording from canonical ids (mirrors the `Source Ledger`'s
`Template label` vs `Matched label`). Phase 0 ships a **seed** + a fuzzy matcher
(reuse `rapidfuzz`, already a dependency); growth is data-driven later.

```python
# ontology.py
class MetricOntology:
    def canonical(self, label: str, *, sheet: str | None = None) -> str | None:
        """Normalize a raw label to a canonical metric id, or None if unknown.
        Order: exact alias → normalized exact → fuzzy (>= threshold) → None."""
    def aliases(self, metric: str) -> list[str]: ...

SEED_ALIASES = {
    "revenue": ["revenue from contracts with customers", "net sales", "turnover"],
    "cost_of_sales": ["cost of sales", "cost of goods sold"],
    "gross_profit": ["gross profit"],
    "operating_profit": ["operating profit", "profit from operations"],
    "finance_cost": ["finance cost", "finance costs"],
    "pat": ["profit after tax", "profit after tax for the year", "profit for the year"],
    "total_assets": ["total assets"],
    "total_equity": ["total equity", "share capital and reserves"],
    "non_current_assets": ["non-current assets", "total non-current assets"],
    "current_assets": ["current assets", "total current assets"],
    # … extended incrementally
}
```

Unmapped labels are **not dropped** — the `FactRef` keeps `metric=None` and `label`
intact, so nothing is lost; `coverage()` reports the unmapped count.

---

## 8. Ingestion pipeline (ordered steps)

```
from_workbook(path)
  1. open workbook (openpyxl, data_only=True) + load sibling manifest json
  2. classify every sheet (§2.1)
  3. for each statement/detail sheet:
       a. detect header row (year integers) → column→year map
       b. detect band row (Historical/Forecasted) → column→period_type map
       c. walk data rows: skip sections/blanks; emit one record per (row, year-col)
       d. attach nearest section label; map label→metric via ontology
  4. concat → findata DataFrame (long); build indices
  5. parse Source Ledger + Validation Ledger → DataFrames; build indices
  6. parse Insights + Insights Review → list[dict]; assign insight_id
  7. assemble FinancialFactStore; compute coverage()
```

Parsers are pure functions (`parse_statement_sheet(ws) -> list[dict]`, etc.) so each is
unit-testable in isolation.

### 8.1 Implementation note — uncalculated formula cells (added during build)

The delivered workbooks were **never recalculated in Excel**, so all statement
formula cells (`P&L`: 81, `Balance Sheet`: 166 — e.g. `=F6+F7`,
`=SUM('PL2 - Cost of Sales'!B5:B20)`) have **no cached value** under
`data_only=True`. This means headline derived lines (`gross_profit`, `pat`,
`total_assets`, `total_equity`, …) read as `None`.

Fix: a lightweight intra-workbook evaluator [`ingest/formulas.py`](../backend/app/engines/fie/ingest/formulas.py)
loads a second `data_only=False` handle and computes effective values — supporting
the only constructs used (cell refs incl. sheet-qualified, ranges, `SUM`, unary
minus, `+ - * /`, parentheses), memoized with cycle detection. The statement parser
falls back to it whenever a cached value is `None`.

Verified: computed `gross_profit`/`operating_profit`/`total_assets` for FY2025 tie
to audited face truths exactly. (Where the workbook's own formula yields a value that
disagrees with face truth — e.g. `pat` 2025 = 4,998,020 vs 6,372,928 — runtime trusts
the workbook per §0.3 and the **dev validator** flags it; this is not an evaluator bug,
both `operating_profit` and `gross_profit` upstream tie out.)

---

## 9. Module layout

```
backend/app/engines/fie/
  models.py        # FactRef, EvidenceItem, Citation, enums          (Task 0.1)
  ontology.py      # MetricOntology + seed aliases + detail map        (Task 0.4)
  ingest/
    classify.py    # sheet classification                              (Task 0.2)
    statements.py  # parse_statement_sheet / parse_detail_sheet        (Task 0.2)
    ledgers.py     # parse_source_ledger / parse_validation_ledger     (Task 0.3)
    insights.py    # parse_insights                                    (Task 0.3)
  store.py         # FinancialFactStore (assembly + lookups + cite)    (Task 0.5)
tests/fie/
  fixtures/golden_millat.py   # audit-derived values (Phase G)         (Task 0.7)
  test_ingest_statements.py
  test_ingest_ledgers.py
  test_store_lookup.py
  test_provenance.py
  test_smoke_workbooks.py
```

---

## 10. Task breakdown & acceptance criteria

| Task | Deliverable | Acceptance criteria |
|---|---|---|
| **0.1** Core types | `models.py` | `FactRef/EvidenceItem/Citation` round-trip `model_dump()`/`model_validate()`; enums enforced. |
| **0.2** Statement+detail parsers | `classify.py`, `statements.py` | Header & band rows auto-detected for `P&L` (row 4, C→2021) **and** `PL1` (row 3, B→2021); section labels attached; section/blank rows excluded. |
| **0.3** Ledger & insight parsers | `ledgers.py`, `insights.py` | `Source Ledger` → 660 rows × 12 cols; `Validation Ledger` → 55 rows; `Insights` → 104 records each with `insight_id` + 7 fields. |
| **0.4** Metric ontology | `ontology.py` | Seed aliases resolve the §7 metrics; fuzzy matcher handles label variants; unmapped → `None` (not error). |
| **0.5** Store assembly + lookups | `store.py` | `from_workbook` builds `findata` long DF with the §3.1 schema; `lookup("revenue", 2024)` returns the authoritative `FactRef` (value 91,534,501, `period_type=historical`). |
| **0.6** Provenance `cite()` | `store.py` | `cite()` resolves tier-1 for a detail row and tier-2 (`via_detail`) for headline `revenue`; returns ≥1 `Citation` with `report_file/page`; uncovered → `provenance_basis="none"`. |
| **0.7** Golden fixtures | `fixtures/golden_millat.py` | Phase-G values importable (authoring only; assertions run in the Phase 2.8 validator). |
| **0.8** Smoke + coverage | `test_smoke_workbooks.py` | Both `millat_*` and `lucky_*` load; `years == [2021..2030]`; `coverage()` reports 0 unexpected parse errors; every **headline** metric is either citeable or listed in the uncovered report. |

Tasks 0.1→0.5 are sequential; 0.6 depends on 0.3+0.5; 0.7 is independent; 0.8 last.

---

## 11. Edge cases & decisions

- **Per-sheet header/band detection** (not hardcoded) — proven necessary by the
  P&L-vs-PL1 offset difference (§2.2/2.3).
- **Sign convention** — values stored as-is (`cost_of_sales` negative). Phase 0 does
  **not** flip signs; the formula layer (L4) owns sign policy.
- **Forecast columns** — `period_type="forecasted"` rows are ingested even when empty
  (value `None`); they define the forecast surface for Phase 4.
- **Empty / merged / section cells** — skipped; never emitted as `value=0`.
- **Encoding artifacts** — raw labels contain mojibake (e.g. `Levy � final taxes`);
  store the raw label, normalize only for ontology matching (don't mutate source text).
- **Numeric coercion** — strings like `"(1,234)"` / `"–"` → parse to `-1234` / `None`;
  log uncoercible cells in `coverage()`.
- **Restatement** — keep all candidate `Source Ledger` rows per `(metric, year)`; tag
  newest `report_year` primary; **do not resolve** (that's L6).
- **One company per workbook** — keyed by `manifest.company` so multi-workbook
  peer-comparison composes later without schema change.

---

## 12. Test plan

- **Unit (pure parsers):** synthetic mini-sheets exercising header detection, band
  detection, section attachment, sign/format coercion, separator skipping.
- **Integration (real workbooks):** load both delivered workbooks; assert sheet counts,
  `findata` row counts within tolerance, ledger dimensions, insight counts.
- **Provenance:** assert tier-1 for a known detail cell and tier-2 for headline
  `revenue` 2024; assert an intentionally-uncovered metric yields `provenance_basis="none"`.
- **Golden authoring (Phase G):** fixtures import cleanly; **assertions deferred** to the
  Phase 2.8 validator (runtime trusts the figures — §0.3).

---

## 13. Exit criteria (Definition of Done for Phase 0)

1. `FinancialFactStore.from_workbook()` loads **both** delivered workbooks without error.
2. `findata` long DataFrame conforms to §3.1; `years` discovered as `[2021..2030]` with
   correct `period_type` banding.
3. `lookup("revenue", 2024)` → `FactRef(value=91_534_501, level="headline", period_type="historical")`.
4. `cite()` returns a resolvable `Citation` (report_file + page) for headline `revenue`
   via the detail bridge; uncovered figures are reported, never silently zero-filled.
5. Insights available as JSON records with `insight_id`; ledgers + manifest loaded.
6. Unit + smoke tests green; `coverage()` clean. **Layer interfaces frozen** for Phase 1.

---

## 14. Open questions / assumptions

- **A1. [RESOLVED]** `cite()` tier-2 for a derived headline total now: (1) filters
  candidate detail rows to the **newest report year** — since the displayed headline
  value reflects the newest report, citing older-report rows would misattribute a
  restated total; and (2) **collapses** the remaining rows to distinct source locations
  `(report_file, page, table_id)`, annotating each citation with `derived_from_rows`.
  This replaced the original aggregate-one-cite-per-line behavior (e.g., current-ratio
  citations dropped 41 → 11; revenue-2024 → 1). A single misleading "representative" row
  was rejected because derived totals (e.g. `current_assets`) have no single source row.
  Locked by `tests/fie/test_provenance.py::test_via_detail_citations_are_collapsed_and_newest_report`.
  *Note:* this affects only **emitted citations**; restatement detection (L6, Phase 2)
  reads the full Source Ledger directly and is unaffected.
- **A2.** `Mgmt info.` / `Qualtitative Data` are title-only in current workbooks → skipped
  in P0. If they later carry structured shareholder/management data, add a parser
  (non-breaking; new `level` value).
- **Q1.** Confirm fiscal-year labeling convention (calendar vs fiscal end) for display —
  affects `Citation.display` strings only, not identity.
