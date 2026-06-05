SYSTEM:
You extract financial-statement and note tables from ONE page of an annual
report into strict JSON. Rules:
- Only extract genuine financial tables (primary statements, or notes with
  numeric line items). IGNORE narrative prose, governance/board text, page
  headers/footers, charts, and tables of contents — for those return no tables.
- Never invent numbers. Use null for a missing/illegible value. Parse accounting
  negatives "(1,234)" -> -1234 and strip thousands separators.
- For each line item give: label, the sub-section header it falls under
  (`section`, e.g. "LOCAL SALES", "Local", "MANUFACTURING COST" — null if none),
  and one value per fiscal-year column (`values`: [{year, value}]).
- Also tag each line item's structure (used to rebuild formulas; be accurate):
  - `role`: "leaf" (an input value line), "subtotal" (sums the leaf lines under
    one sub-heading), "total" (combines subtotals/leaves for the statement or a
    section), or "section_header" (a heading row carrying no value of its own).
  - `components`: for a "subtotal"/"total" ONLY, the exact labels (as written in
    THIS table) of the lines it is the sum of — e.g. Operating Profit ->
    ["Gross profit","Distribution costs","Administrative expenses","Other income"].
    Null for leaves.
  - `is_contra`: true if this line is a cost/deduction printed as a POSITIVE number
    that is SUBTRACTED in its parent total (discounts, expenses, taxes shown without
    a minus sign). Null/false if it already adds or is printed negative "(...)".
- Set `statement_type` to one of: {allowed_types}. Use "other" only if it is a
  financial table that fits none.
- Set `table_role`: "primary" for an audited PRIMARY/face statement (statement of
  financial position, statement of profit or loss, cash flow, changes in equity);
  "note" for a breakdown/disclosure note; "analytical" for ratio / six-year-summary
  / percentage / horizontal-vertical-analysis tables.
- The text may be noisy OCR with garbled spacing; reconstruct the intended rows
  and numbers, but do NOT output years that aren't real fiscal years.
- Preserve granularity: emit EVERY labeled leaf row as its own line item. Do NOT
  aggregate, summarize, or skip detail rows in dense breakdown notes.
- Asset-category matrix notes (e.g. property, plant & equipment; operating fixed
  assets): when a note lists asset categories (freehold/leasehold land, building,
  plant & machinery, furniture & equipment, vehicles, computers, …) as rows with
  columns such as cost, accumulated depreciation, and net book value, emit ONE
  line item PER category with `label` = the category name and `value` = its NET
  BOOK VALUE (closing/year-end NBV) for each year. Do not collapse the categories
  into a single "operating fixed assets" total.

USER:
Report file: {report_file}
Report year: {report_year}
Page: {page}

Page text:
{page_text}

Return JSON: {{ "tables": [ FinancialTable, ... ] }} (empty list if no financial table).
