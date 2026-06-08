# OCR Extraction Pipeline — Layer Guide (PDF → XLSX)

A companion to the pipeline diagram. For each layer: **what happens (plain English)**,
**key terms explained**, and the **exact structure of its output**.

---

## Shared building block — `SourceRef` (provenance = "where did this number come from")

Almost every artifact carries this so any value can be traced back to the page it came from.

```
SourceRef:
  report_file   "Lucky-Cement 2024.pdf"      # which PDF
  report_year   2024                          # that report's year
  pages         [12, 13]                      # 1-based page(s) the data spans
  section       "LOCAL SALES" | null          # sub-heading it sat under
  table_id      "Lucky-Cement 2024.pdf:p12:0" # stable id of the source table
  table_title   "Statement of Financial Position" | null
```

---

## Layer 1 — Ingestion / OCR   ·   *Rule-Based*

**What happens:** Open each PDF and pull the text out of every page. Pages that already
contain real text ("native") are read directly. Pages that are just a scanned **image**
have no text, so each is rendered to a picture and run through **OCR** to recover the words.

**Terms:**
- **Native text** — text embedded in the PDF (selectable/copyable). Read directly: fast, exact.
- **OCR (Optical Character Recognition)** — converting a *picture* of text into actual
  characters. Used only for scanned/image pages.
- **Tesseract** — the open-source OCR engine doing that conversion.
- **DPI** — resolution the page image is rendered at before OCR (higher = clearer but
  slower and more memory).

**Output — `IngestedDoc` (one per PDF):**
```
IngestedDoc:
  file_name     "Lucky-Cement 2024.pdf"
  company       "Lucky Cement Limited" | null   # guessed from the cover
  report_year   2024 | null                      # guessed from the cover/filename
  is_scanned    true/false                        # true if most pages needed OCR
  page_count    248
  pages: [ PageText ]
      PageText:
        page            12          # 1-based
        text            "…"         # the page's text (native or OCR'd)
        kind            native | ocr
        char_count      1843
        ocr_confidence  0.0–1.0 | null
        ocr_words       [ {text, x, y, w, h} … ]  # word boxes (used by Layer 2)
```

---

## Layer 2 — Table & Section Detection   ·   *Rule-Based + Embeddings*

**What happens:** Work out **where** the tables are on each page, **what** financial
statement each one is, and **where** the narrative/commentary sections are.

**Terms:**
- **Word-grid clustering** — uses the on-page **positions** of words to group them into
  rows and columns by their x/y coordinates. That geometric grid *is* what a table looks
  like. If enough cells contain numbers, it's treated as a table.
- **Numeric ratio** — the share of cells containing digits; a gate so prose isn't mistaken
  for a table.
- **Statement type** — which statement it is (income statement, balance sheet, cash flow,
  a note, etc.).
- **Fuzzy matching** — approximate text match that tolerates typos/OCR noise
  ("Proft" ≈ "Profit").
- **Embeddings** — a small **local** AI model turns a heading into a vector of numbers;
  similar *meanings* land close together, so "Statement of Financial Position" matches
  "Balance Sheet" even with different words. *Local* = runs on your machine, free, **not**
  an LLM API call.
- **Narrative sections** — the prose parts (CEO review, MD&A, outlook) kept for Layer 3 insights.

**Output — `TableSet` (one per PDF):**
```
TableSet:
  file_name, report_year
  sections: [ Section ]
      Section: statement_type, title, start_page, end_page, consolidated
  tables:   [ RawTable ]
      RawTable:
        table_id            "…:p12:0"
        statement_type      balance_sheet | income_statement | … | other
        title               "Statement of Financial Position"
        header              ["Particulars","2024","2023"]
        rows                [["Cash","1500","1320"], …]   # raw cells
        years               [2024, 2023]
        currency, unit_scale
        orientation         vertical | horizontal | unknown
        needs_review        true  → hand this page to GPT in Layer 3
        classification_method / classification_score
        consolidated        true/false/null
        from_ocr            true/false
        source              SourceRef
```

---

## Layer 3 — Interpretation   ·   *Hybrid (LLM + Rule-Based)*  ← the core

**What happens:** For each financial page, **GPT** reads the page text (and, optionally,
the page **image**) and returns one clean, structured table — every row with its label,
its value per year, and structural tags. Separately, GPT reads the narrative and produces
business **insights**. A deterministic rule pass then cleans signs and footnote markers.

**Terms:**
- **LLM / GPT** — the large language model (`gpt-5.4-mini`) doing the structuring.
- **Strict JSON / structured output** — GPT is *forced* to answer in an exact shape (a
  schema), never free text.
- **Vision** — sending the page **image** alongside the text so the model can settle OCR
  ambiguity (exact digits, minus signs, column alignment) by looking at the real page.
- **Line item** — one row of a statement (e.g. "Revenue", "Total assets").
- **role** — the row's structural job: `leaf` (a raw input line), `subtotal` (sums some
  leaves), `total` (a statement/section total), `section_header` (a heading with no value).
  Used later to **rebuild Excel formulas**.
- **components** — for a subtotal/total, the exact rows it adds up → lets Excel re-create
  the `SUM`.
- **is_contra** — a cost/deduction printed as a *positive* number that must be **subtracted**
  in its parent (e.g. expenses, taxes shown without a minus).
- **canonical_metric / normalization** — mapping a company's wording ("Turnover") to a
  standard name ("Revenue") so companies are comparable. (Filled by a later resolver —
  `null` at extraction time.)
- **table_role** — `primary` = the audited face statement; `note` = a breakdown/disclosure;
  `analytical` = ratios / six-year summary.
- **Insight** — a one-sentence business takeaway + its source section, page, and confidence.

**Output — `DocumentResult` (one per PDF):**
```
DocumentResult:
  file_name, company, report_year
  tables: [ FinancialTable ]
      FinancialTable:
        statement_type, title, currency, unit_scale
        consolidated      true/false/null
        table_role        primary | note | analytical
        years             [2024, 2023]
        source            SourceRef
        line_items: [ LineItem ]
            LineItem:
              label              "Revenue from contracts with customers"
              section            "LOCAL SALES" | null
              unit               "PKR thousands" | null
              note_ref           "24" | null
              role               leaf | subtotal | total | section_header
              components         ["Gross profit","Distribution costs", …] | null
              is_contra          true/false/null
              canonical_metric   "revenue" | null     # set later, Layer 5
              canonical_category | resolution | quarantine_reason
              values: [ LineItemValue ]
                  LineItemValue:
                    year                2024
                    value               62108997     # parsed number (negatives for "(…)")
                    raw                 "62,108,997"  # original printed text
                    source_report_year  2024
                    source              SourceRef     # ← powers PDF page-sync
  insights:        [ Insight ]   # confidence ≥ review threshold
  insights_review: [ Insight ]   # lower-confidence bucket
      Insight: year, source_report_year, area, takeaway, source_section, page, confidence
```

---

## Layer 4 — Multi-Year Resolution   ·   *Rule-Based*

**What happens:** You usually feed several years of reports. Each report has, say, a balance
sheet. This **merges** all of them into **one** balance sheet with a column per year, lining
up the same rows across reports — then fixes splits, removes duplicates, handles
restatements, and sets aside (quarantines) lines that can't be trusted.

**Terms:**
- **Merge across reports/years** — combine the 2023/2024/2025 versions of the *same*
  statement into one multi-year table.
- **Consolidate split leaves** — the same line can be split differently between reports
  ("section drift"); this recovers them as a single row.
- **Dedup** — drop duplicate rows.
- **Restatement** — when a later report revises a prior year's figure; the newest/
  authoritative value wins.
- **Quarantine** — malformed/untrustworthy lines are set aside (kept in `rejected_lines`,
  never silently dropped).
- **Provenance** — every value still remembers its file/page (`SourceRef`).

**Output — `CompanyResult` (one per run):**
```
CompanyResult:
  company           "Lucky Cement Limited"
  fiscal_years      [2023, 2024, 2025]      # all data years, ascending
  source_reports    ["Lucky-Cement 2023.pdf", …]
  tables            [ FinancialTable ]      # now MULTI-YEAR merged (same shape as Layer 3)
  insights, insights_review
  rejected_lines: [ RejectedLine ]
      RejectedLine: label, statement_type, canonical_metric, reason, source
```

---

## Layer 5 — Face Truth & Template Mapping   ·   *Rule-Based + Embeddings*

**What happens:** Two jobs. **(a)** Decide the single audited **"truth"** value for each
metric/year — the number that *must* be right. **(b)** Put the data into the output: either
fill the client's Excel **template**, or build a fresh workbook (one sheet per table).

**Terms:**
- **Face truth** — the authoritative value for a `(metric, year)`, chosen when there are
  several candidates (consolidated vs unconsolidated, primary statement vs a note).
- **Basis (consolidated vs unconsolidated)** — two versions of the accounts; the pipeline
  keeps a consistent basis (these templates prefer **unconsolidated**).
- **Accounting-identity reconciliation** — sanity checks using accounting rules:
  *Profit-after-tax = Profit-before-tax − tax*; *Assets = Equity + Liabilities*; cash
  roll-forward. A candidate that breaks an identity is treated as suspect.
- **Currency anchor / magnitude-outlier guard** — reject a value that's off by a wrong
  scale (e.g. 1000× from a units slip).
- **Template mapping** — match each template **row label** to an extracted line item and
  write its value into the correct year cell.
- **Family gate** — only allow a match within the **same statement family** (a balance-sheet
  line can't land in a P&L row).
- **Formulas preserved** — the template's own formulas (subtotals, cross-sheet pulls) are
  never overwritten; only **empty input cells** get values.

**Outputs:**
```
FaceTruth (in-memory lookup):
  { (metric, year) -> (value, SourceRef) }     # e.g. ("revenue", 2024) -> (91534501, …)

MappingPlan:
  writes: [ CellWrite ]
      CellWrite:
        sheet               "BS"
        coordinate          "C7"
        year                2024
        value               1500.0
        template_label      "Cash and bank balances"   # the template's row
        matched_label       "Cash & bank"              # the extracted line it matched
        confidence          0.0–1.0
        source_report_year, source(SourceRef)
        note                "withheld:tieout" | null
  formula_sheets            ["P&L","BS"]   # computed/output sheets (not written into)
  sheets_processed / sheets_skipped
  unmatched_template_labels [ … ]          # template rows nothing mapped to
  withheld: [ CellWrite ]                  # values held back (conflicted with face truth)

Workbook (in progress):  template filled (formulas intact)  OR  one styled sheet per table
```

---

## Layer 6 — Assembly, Validation & Export   ·   *Rule-Based*

**What happens:** Finish the workbook and *prove it's correct*. Repair template formula
defects, substitute audited truth into headline cells that don't add up, reconcile
breakdowns honestly, write audit trails, and embed provenance.

**Terms:**
- **Tie-out** — does the computed total equal the audited total? A subtotal that doesn't
  tie out is flagged or corrected.
- **Headline override** — for P&L / Balance-Sheet output cells, if the formula result
  disagrees with the audited face truth, substitute the audited number (with a cell
  comment) so the delivered statement is right.
- **Materiality-gated reconciliation** — only "plug" **small** gaps (≤5% — rounding or a
  little missing detail); **large** gaps are disclosed as `DETAIL_INCOMPLETE`, never faked.
- **Validation Ledger / Source Ledger** — audit sheets: each value's status
  (ok / mismatch / override) and its report/page origin.
- **Scope & Notes** — a first tab stating what's audited-exact vs out of scope.
- **Recalc / fullCalcOnLoad** — tells Excel to recompute formulas on open; optionally
  materializes cached values via LibreOffice.
- **sheet→PDF-page map (`SheetSources`)** — per-sheet provenance embedded as a custom
  workbook property, so a side-by-side viewer can jump the PDF to the right page.

**Outputs:**
```
<output>.xlsx
  • Data sheets        — filled template, OR one styled sheet per table
  • Insights           — business takeaways (area, takeaway, section, page, confidence)
  • Insights Review    — lower-confidence insights
  • Validation Ledger  — per-value status vs audited truth
  • Source Ledger      — per-cell origin (report / page / table)   [template mode]
  • Scope & Notes      — delivery scope + what's incomplete
  • (embedded) custom property "SheetSources" = the sheet→page map

<output>.xlsx.manifest.json   (sidecar)
  company, mode (template | no_template), fiscal_years, source_reports
  counts:  writes, withheld, quarantined_lines, validation_failures,
           detail_reconciliation "ok/checked", breakdown_reconciled,
           detail_incomplete, identity_failures, headline_overrides,
           template_formulas_repaired
  flags:   production_ready, fully_reconciled,
           cash_flow_in_scope, formula_cache_materialized
  sheet_sources: { "<sheet>": [ {report_file, pages, table_ids, weight} … ] }
```

---

### One-line summary of the data hand-off
`PDF(s) → IngestedDoc → TableSet → DocumentResult ×N → CompanyResult → (FaceTruth + MappingPlan + Workbook) → .xlsx + manifest.json`
