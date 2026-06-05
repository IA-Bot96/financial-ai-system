# Template Formula Specification

**Purpose.** This document catalogs every formula used in the supplied financial-model
templates (`millat-template.xlsx`, `lucky-template.xlsx`) and restates them
**semantically** (in terms of *what is added to what*, not fixed cell ranges).

It is the reference for the **no-template Excel writer**: when no template is
supplied, the generated workbook should reproduce these subtotal / total /
cross-statement formulas *by meaning* — e.g. *"Total Assets = sum of all asset
line items"* — rather than as opaque cell ranges. The same identities double as
**tie-out validation** (a computed total that disagrees with the report's stated
total is a flag).

Both templates produce the same conceptual model; differences are noted inline.

---

## 1. Formula taxonomy

| # | Type | Example (cell) | Semantic meaning |
|---|------|----------------|------------------|
| 1 | **Subtotal (range SUM)** | `=SUM(B5:B9)` | Sum of the contiguous **leaf** rows under one sub-heading |
| 2 | **Intra-sheet derived** | `=B18+B25`, `=$B$10+$B$17` | Combine subtotals within a sheet (often `+` with one term already negative) |
| 3 | **Section total** | `=B7+B30+B35+…` | Sum of the sub-section subtotals on a breakdown sheet |
| 4 | **Cross-sheet pull** | `='PL1 - Revenue'!B29`, `=-'PL2 - Cost of Sales'!B47` | Output statement pulls a breakdown total; **sign-flipped** for costs/expenses/levy/tax |
| 5 | **Cross-sheet identity** | `=C14+C26` (Total Assets) | Accounting identity over pulled values |

**Conventions**
- Every formula is **replicated across each historical year column** (B…F = 2021…2025). Generate the same formula per filled year column.
- **Leaf rows** hold input values; **subtotal/total rows** hold formulas — never overwrite a formula row with a value.
- **Sign convention:** breakdown sheets store costs/expenses/deductions as **positive** magnitudes (or with their own internal sign); the **output P&L negates** them when pulling (`=-'PL2…'!…`). Deduction lines on PL1 are stored **negative** and *added*.
- A trailing **balance check** row (`=Total Assets − Total Equity & Liabilities`, expect `0`) appears on the Balance Sheet.

---

## 2. Generation rules for the no-template path

To apply these semantically when generating a sheet:

1. **Subtotal (type 1):** for a "Total/Gross/Net <section>" row, `= SUM(` the leaf
   rows whose `section` == that sub-heading `)`. Group leaves by `LineItem.section`
   (or the contiguous block since the previous heading/subtotal).
2. **Section total (type 3):** the sheet's grand total `= ` sum of the section
   subtotal rows (e.g. `TOTAL NON-CURRENT ASSETS = PP&E + ROU + Investment property
   + … `).
3. **Cross-statement identities (types 4–5):** use the **canonical-metric → cell**
   map (each line resolves to a canonical metric) and the identity library in
   §5. Negate cost/expense/finance/levy/tax terms.
4. **Tie-out:** after writing a subtotal/total formula, compare its computed value
   to the report's stated total (if extracted). Mismatch ⇒ flag (validation).

---

## 3. P&L breakdown sheets

### PL1 — Revenue
| Result row | Semantic formula |
|---|---|
| Gross Local Sales | Σ local product lines (Tractors, Implements, …) |
| Total Deductions | Σ deduction lines (trade discount, sales tax, …) *(stored negative)* |
| Net Local Sales | Gross Local Sales **+** Total Deductions |
| Total Export Sales | Σ export product lines *(Millat)* |
| Gross Revenue (Local + Export) | Net Local Sales **+** Total Export Sales *(Millat)* |
| **Net Revenue from Contracts** | Gross Revenue **+** (Less: Commission) |

*Lucky variant:* `Gross Revenue = Local + Export`; `Total Deductions = Σ(sales tax, rebates)`; `Net Revenue = Gross Revenue + Total Deductions`.

### PL2 — Cost of Sales
| Result row | Semantic formula |
|---|---|
| Total Manufacturing Cost | Σ manufacturing cost lines |
| Net WIP movement | Σ (opening WIP, transfers, − closing WIP) |
| Cost of goods manufactured | Total Manufacturing Cost **+** Net WIP movement |
| Net FG movement | Σ (opening FG, transfers, − closing FG) |
| Cost of sales — manufactured | Cost of goods manufactured **+** Net FG movement |
| Components consumed *(Millat note 33.1)* | Total available (opening + purchases) **+** (− closing) |
| Cost of sales — trading *(Millat)* | Total available **+** (− closing) |
| **TOTAL COST OF SALES** | Cost of sales–manufactured **+** Cost of sales–trading |

*Lucky variant:* `TOTAL COST OF SALES = Cost of goods manufactured + Net Finished Goods Movement`.

### PL3 — Operating Expenses
| Result row | Semantic formula |
|---|---|
| Total Distribution & Marketing Expenses | Σ distribution leaf lines |
| Total Administrative Expenses | Σ administrative leaf lines |
| Total Other Operating Expenses | Σ other-operating leaf lines |
| **TOTAL OPERATING EXPENSES** | Distribution **+** Administrative **+** Other operating *(Millat)* |

> ⚠️ Distribution and Administrative share leaf labels (Salaries, Contract services,
> Fuel, Communication, Travelling). Each subtotal must sum **only its own section's**
> leaves — group by `section`, not by label.

### PL4 — Other Income
| Result row | Semantic formula |
|---|---|
| Total — Income from Financial Assets | Σ financial-asset income lines |
| Total — Income from Subsidiaries *(Millat)* | Σ subsidiary dividend lines |
| Total — Non-Financial Asset Income *(Lucky)* | Σ non-financial asset income lines |
| Total — Other Asset Income *(Millat)* | Σ other-asset income lines |
| **TOTAL OTHER INCOME** | sum of the section subtotals above |

### PL5 — Finance Cost
| Result row | Semantic formula |
|---|---|
| **TOTAL FINANCE COST** | Σ all finance-cost lines |

### PL6 — Levy / Taxation
| Result row | Semantic formula |
|---|---|
| Total Levy — Final Taxes | Σ levy/final-tax lines |
| Sub-total / Total Taxation | Current tax **+** Deferred tax |
| Total Income Tax *(Millat)* | Sub-total **+** prior-year tax |
| **TOTAL LEVY AND TAX CHARGED** *(Lucky)* | Levy **+** Total Taxation |

### PL7 — Other Comprehensive Income
| Result row | Semantic formula |
|---|---|
| Net (after tax) | Σ (item, − deferred tax) — one block per OCI item |
| **Total Other Comprehensive Loss** | sum of the net-after-tax blocks |
| Total Comprehensive Income for the year | Profit after tax **+** Total Other Comprehensive Loss *(Millat)* |

---

## 4. Balance-sheet breakdown sheets

### BS1 — Non-Current Assets
| Result row | Semantic formula |
|---|---|
| Total PP&E | Operating fixed assets **+** Capital work in progress |
| Total Operating Fixed Assets (NBV) | Σ asset categories by NBV |
| Net Investment Property | Gross **+** (− impairment) |
| Total Intangible Assets | Σ intangible lines |
| Total Long-term Investments | sum of investment sub-totals (subsidiaries + associates + others) |
| Total Long-term Loans and Advances | Σ long-term loan/advance lines |
| **TOTAL NON-CURRENT ASSETS** | PP&E + ROU + Investment property + Intangibles + Long-term investments + Employee benefit asset + Long-term loans |

### BS2 — Current Assets
| Result row | Semantic formula |
|---|---|
| Net Stores & Spares | Gross **+** (− provision for obsolescence) |
| Total Stock-in-trade | Raw material + WIP + Finished goods |
| Total Trade Debts | Σ trade-debt lines |
| Total Loans and Advances | Σ loan/advance lines |
| Total Trade Deposits and Prepayments | Σ deposit/prepayment lines |
| Total Other Receivables | Σ other-receivable lines |
| Total Cash and Bank Balances | cash-in-hand sub-total **+** bank-balances sub-total |
| **TOTAL CURRENT ASSETS** | Stores + Stock-in-trade + Trade debts + Loans & advances + Deposits + Other receivables + Taxation + Statutory balances + Cash & bank |

### BS3 — Share Capital & Reserves
| Result row | Semantic formula |
|---|---|
| Total Issued, Subscribed & Paid-up Capital | Paid-in capital **+** Total bonus shares |
| Total Capital Reserves | Σ capital-reserve lines |
| Total Revenue Reserves | General reserve **+** Unappropriated profit |
| TOTAL RESERVES | Total Capital Reserves **+** Total Revenue Reserves |
| **TOTAL EQUITY (Capital + Reserves)** | Paid-up Capital **+** Total Reserves |

### BS4 — Non-Current Liabilities
| Result row | Semantic formula |
|---|---|
| Net Long-term Finances (non-current) | Gross long-term loan **+** (− current portion) |
| Closing total deferred grant | Σ (opening, additions, − transfers) |
| Non-current lease liabilities | Total lease liabilities **+** (− current portion) |
| Net Deferred Tax Liability | Total taxable differences **+** Total deductible differences |
| **TOTAL NON-CURRENT LIABILITIES** | Long-term finances + Deferred grant + Lease liabilities + Long-term deposits + Net deferred tax |

### BS5 — Current Liabilities
| Result row | Semantic formula |
|---|---|
| Total Trade and Other Payables | Σ payable lines |
| Total Short-term Borrowings | Σ short-term borrowing lines |
| Total Current Portion of NCL | Σ current-portion lines |
| Other Current Liabilities | Σ other current-liability lines |
| **TOTAL CURRENT LIABILITIES** | Trade payables + Contract liabilities + Short-term borrowings + Current portion of NCL + Other current liabilities |

---

## 5. Output statements — cross-sheet identities (portable / canonical)

These are the **standard accounting identities** to generate on the consolidated
P&L and Balance Sheet output sheets. Keyed on canonical metrics so they apply to
any company. Negate cost/expense/finance/levy/tax terms (stored positive on the
breakdown sheets).

### Income Statement
```
gross_profit            = revenue − cost_of_sales
total_operating_expenses= distribution + administrative + other_operating
operating_profit        = gross_profit − total_operating_expenses + other_income
profit_before_tax_levy  = operating_profit − finance_cost
profit_before_tax       = profit_before_tax_levy − levy
profit_after_tax        = profit_before_tax − taxation
total_comprehensive_income = profit_after_tax + total_other_comprehensive_income
```
*(Lucky orders it as `Profit before taxation and levy = SUM(gross_profit, −distribution, −admin, −finance, −other_expenses, +other_income)`; the net result is identical.)*

### Balance Sheet
```
total_non_current_assets   = Σ non-current-asset line pulls
total_current_assets       = Σ current-asset line pulls
TOTAL ASSETS               = total_non_current_assets + total_current_assets

total_equity               = share_capital + reserves
total_non_current_liab     = Σ non-current-liability pulls
total_current_liab         = Σ current-liability pulls
TOTAL EQUITY & LIABILITIES = total_equity + total_non_current_liab + total_current_liab

balance_check              = TOTAL ASSETS − TOTAL EQUITY & LIABILITIES   # must be 0
```

---

## 6. Tie-out / validation checks (free byproduct)

Generating these formulas yields built-in validation:
- **Balance check:** `TOTAL ASSETS − TOTAL EQUITY & LIABILITIES == 0`.
- **Subtotal tie-out:** `SUM(section leaves)` should equal the report's stated
  subtotal (if extracted). Mismatch ⇒ extraction gap / wrong sign — flag it.
- **Statement tie-out:** `revenue − cost_of_sales == gross_profit`, etc.

---

## 7. Caveats for the generator

1. **Hierarchy is inferred.** The extracted `FinancialTable` is a flat list; group
   leaves into subtotals via `LineItem.section` (preferred) or the contiguous block
   since the last heading/subtotal. Misgrouping → wrong formula.
2. **Repeated labels across sub-sections** (PL3 distribution vs administrative) must
   be grouped by `section`, never by label.
3. **Sign handling** must be consistent: deductions/costs negated where the identity
   expects it; otherwise totals break.
4. **Unambiguous references** require each canonical metric to appear once per
   (sheet, year); duplicates need disambiguation before emitting cross-sheet formulas.
5. **Formula vs reported value:** prefer emitting the **formula** (self-validating),
   but record the report's stated total alongside so a mismatch is visible rather
   than silently overwritten.
