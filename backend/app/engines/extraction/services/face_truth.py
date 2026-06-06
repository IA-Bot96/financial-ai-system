"""Trusted face-statement truth + table-role inference (validation primitives P1/P3).

`TrustedFaceIndex` is the single source of truth used to validate emitted values
(template and no-template) against the audited primary statements. It is built
ONLY from `table_role == 'primary'` face tables, with currency-scale and
magnitude-sanity filters and explicit conflict resolution — so a ratio row, a
six-year-summary cell, or a mislabelled note can never become "truth".
"""
from __future__ import annotations

import re
import statistics
from typing import Optional

from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.financials import FinancialTable, LineItem

# Face (primary) statement types.
FACE_TYPES = {
    StatementType.income_statement, StatementType.balance_sheet,
    StatementType.cash_flow, StatementType.equity_changes,
}

# Headline statement totals: a populated value for one of these must reconcile to
# the audited face statement, or it is withheld (never silently shipped wrong).
KEY_METRICS = frozenset({
    "revenue", "cost_of_sales", "gross_profit", "operating_profit",
    "profit_before_tax", "profit_after_tax", "total_assets", "total_liabilities",
    "equity", "total_equity_and_liabilities", "finance_cost", "taxation",
    "income_tax", "tax_expense", "non_current_assets", "current_assets",
    "non_current_liabilities", "current_liabilities", "other_income",
})

# Canonical metrics that name the SAME face concept — folded together for face truth so
# a value under either name supplies the shared metric's truth. "Share capital and
# reserves" is the total-equity line in many statements (= equity); verified equal where
# both appear. Only the alias TARGET is a KEY metric, so lookups stay consistent.
_METRIC_ALIASES = {
    "share_capital_and_reserves": "equity",
}

# Expense/deduction metrics are ALWAYS negative on an additive P&L. Sources report them
# inconsistently (a cost is positive in a cost note, parenthesised in the statement), so
# face truth is normalised to -abs for these — keeping it consistent with the output
# statement's formula sign (=-'PL2'!.. ) so the signed tie-out passes. Profit subtotals
# and other_income are NOT here (they can legitimately be negative / vary). #6.
_EXPENSE_KEY_METRICS = frozenset({"cost_of_sales", "finance_cost", "tax_expense", "taxation", "income_tax"})

# Metrics that LEGITIMATELY appear across statement families — never quarantined
# (deferred tax on the BS, depreciation in PP&E notes, finance-cost-paid in cash flow…).
CROSS_FAMILY_OK = frozenset({
    "deferred_tax", "deferred_tax_liability", "depreciation_expense", "depreciation_pp&e",
    "depreciation_right_of_use_assets", "depreciation_related_parties", "amortization_expense",
    "finance_cost_paid", "income_taxes_paid", "workers_profit_participation_fund",
    "workers_welfare_fund", "dividends_paid",
})

# canonical_category -> statement family.
_CATEGORY_FAMILY = {"income_statement": "income", "balance_sheet": "balance", "cash_flow": "cash"}
# statement_type -> statement family.
_TYPE_FAMILY = {
    StatementType.income_statement: "income", StatementType.revenue: "income",
    StatementType.cost_of_sales: "income", StatementType.operating_expenses: "income",
    StatementType.other_income: "income", StatementType.finance_cost: "income",
    StatementType.taxation: "income", StatementType.oci: "income",
    StatementType.balance_sheet: "balance", StatementType.non_current_assets: "balance",
    StatementType.current_assets: "balance", StatementType.share_capital_reserves: "balance",
    StatementType.non_current_liabilities: "balance", StatementType.current_liabilities: "balance",
    StatementType.equity_changes: "balance", StatementType.cash_flow: "cash",
}


def tieout(value: float, truth: float) -> bool:
    """A populated value agrees with the audited face value within ROUNDING (1% + abs
    epsilon), not the old loose 5%. A face-statement figure should reconcile near-exactly;
    a 1.4% gap (e.g. revenue 52.85M vs 52.11M) is a real error, not rounding, and the old
    5% band silently passed it. Genuine rounding/restatement stays inside 1%; gross
    mis-maps and digit slips are far outside. Matches the identity-check tolerance."""
    return abs(value - truth) <= max(1.0, 0.01 * abs(truth))


def metric_incompatible(metric: str | None, category: str | None,
                        statement_type: StatementType | None) -> bool:
    """True when `metric` (in `category`) is CONFIDENTLY in a different statement
    family than `statement_type`. Cross-family metrics and unknown families are
    exempt (never blocks on a missing signal)."""
    if not metric or metric in CROSS_FAMILY_OK:
        return False
    cat_family = _CATEGORY_FAMILY.get(category or "")
    home_family = _TYPE_FAMILY.get(statement_type) if statement_type else None
    return bool(cat_family and home_family and cat_family != home_family)


def confidently_incompatible(li: LineItem, statement_type: StatementType) -> bool:
    """P2: True only when the line's canonical metric is CONFIDENTLY in a different
    statement family than its home table (e.g. a `cost_of_sales`/income line inside
    a balance-sheet table)."""
    return metric_incompatible(li.canonical_metric, li.canonical_category, statement_type)

# STRONG analytical markers: a transformed/summarized view that must NEVER be truth,
# regardless of content (a six-year summary lists real totals but is still a summary).
_ANALYTICAL_RE = re.compile(
    r"six[\s-]?year|ten[\s-]?year|highlight|ratio|horizontal|vertical|"
    r"key\s+(?:data|figures|indicators)|at\s+a\s+glance|common[\s-]?size",
    re.I,
)
# WEAK marker: bare "analysis" is ambiguous. "Analysis of Statement of Financial
# Position" is the statement itself (real absolute figures that balance), whereas
# "Horizontal/Vertical analysis" is already caught by the strong markers above. So
# "analysis" only demotes a table when there is NO corroborating primary face
# evidence (a face title or a headline-total line).
_WEAK_ANALYTICAL_RE = re.compile(r"\banalysis\b", re.I)
# A handful of headline-total metrics — their presence confirms a real face statement.
_FACE_TOTAL_METRICS = {
    "revenue", "gross_profit", "operating_profit", "profit_before_tax", "profit_after_tax",
    "total_assets", "total_liabilities", "total_equity_and_liabilities", "equity",
    "cash_at_end_of_period",
}
# Balance-sheet GRAND totals — only the face statement carries these (a PP&E or payables
# note never does). A balance-family sub-type table holding one is really a split-off
# section of the primary statement (e.g. an "ASSETS - NON-CURRENT ASSETS" half-page the
# classifier typed `non_current_assets`), so it should contribute face truth. Subtotals
# like `current_assets` are deliberately excluded — notes carry those (often wrong).
_BS_GRAND_TOTALS = {"total_assets", "total_equity_and_liabilities", "total_liabilities"}
# Face-statement title keywords.
_FACE_TITLE_RE = re.compile(
    r"statement of financial position|balance sheet|statement of profit|"
    r"profit (?:or|and) loss|income statement|cash flow|changes in equity|"
    r"comprehensive income",
    re.I,
)


def infer_table_role(table: FinancialTable) -> str:
    """'primary' | 'note' | 'analytical'. Used when the extractor/classifier
    didn't set `table_role` (e.g. GPT page tables)."""
    title = (table.title or "").lower()
    # Definite analytical: a highlights type, a %/ratio scale, or a STRONG analytical
    # title (six-year summary, ratios, horizontal/vertical analysis…).
    if (table.statement_type == StatementType.financial_highlights
            or _ANALYTICAL_RE.search(title)
            or (table.unit_scale or "").strip() in {"%", "percent", "ratio"}):
        return "analytical"
    metrics = {li.canonical_metric for li in table.line_items if li.canonical_metric}
    if table.statement_type in FACE_TYPES:
        # A face TYPE alone isn't enough (note pages get mis-typed) — require a face
        # title or a headline-total line as corroboration. This corroboration also
        # OUTRANKS a bare "analysis" in the title, so a real statement titled
        # "Analysis of Statement of Financial Position" is correctly primary.
        if _FACE_TITLE_RE.search(title) or (metrics & _FACE_TOTAL_METRICS):
            return "primary"
    # A balance-family section the classifier split off as a sub-type note but which
    # carries a balance-sheet GRAND total is really part of the primary statement.
    if _TYPE_FAMILY.get(table.statement_type) == "balance" and (metrics & _BS_GRAND_TOTALS):
        return "primary"
    # A bare "analysis" block with no primary face evidence is analytical, not a note.
    if _WEAK_ANALYTICAL_RE.search(title):
        return "analytical"
    return "note"


def table_role_of(table: FinancialTable) -> str:
    return table.table_role or infer_table_role(table)


def _is_currency_scale(table: FinancialTable) -> bool:
    unit = (table.unit_scale or "").strip().lower()
    return unit not in {"%", "percent", "ratio"}


# Reporting basis, read from the table TITLE (reliable) rather than the `consolidated`
# flag (found to be mislabelled in carry-forward). "unconsolidated" is checked first so
# it never matches the "consolidated" substring it contains. Synonyms per the accounting
# glossary: unconsolidated = separate / standalone / parent company / company financial
# statements; consolidated = group financial statements / group accounts. Bare "group" or
# "company" is deliberately NOT matched (far too common in titles to be a basis signal).
_UNCONSOLIDATED_RE = re.compile(
    r"\b(?:un\s*-?\s*consolidated|stand[\s-]?alone|separate|parent\s+company"
    r"|company\s+financial\s+statements)\b", re.I)
_CONSOLIDATED_RE = re.compile(
    r"\b(?:consolidated|group\s+(?:financial\s+statements|accounts))\b", re.I)


def _table_basis(table: FinancialTable) -> str:
    """'unconsolidated' | 'consolidated' | 'unknown', from the statement title."""
    title = table.title or ""
    if _UNCONSOLIDATED_RE.search(title):
        return "unconsolidated"
    if _CONSOLIDATED_RE.search(title):
        return "consolidated"
    return "unknown"


def build_face_truth(tables: list[FinancialTable], prefer_basis: str = "unconsolidated",
                     trace: list | None = None, notes_only: bool = False,
                     ) -> dict[tuple[str, int], tuple[float, object]]:
    """{(canonical_metric, year): (value, source)} from face statements, primary-first.

    Candidates are collected from primary (tier 0) AND note (tier 1) tables; analytical
    /ratio tables are excluded entirely. For each (metric, year) a PRIMARY value always
    wins; a NOTE value is used only as a FALLBACK when no primary candidate exists for
    that pair (e.g. the oldest comparative year, whose primary statement extracted
    poorly but whose disaggregation notes still carry the real total).

    Within the chosen tier: newest report wins; ties broken by closeness to the metric's
    median (never larger magnitude — that would bless an extra-digit error). The
    magnitude-outlier filter's median is anchored to PRIMARY values so a mis-tagged note
    partial can't shift the reference.

    Finally, face truth is made source-consistent for the accounting identities (the
    cash-flow roll-forward and PAT = PBT + tax): a consolidating group files the same
    statement on two bases, and the per-metric selection above can mix them (e.g. an
    unconsolidated PBT/PAT with a consolidated tax, or a consolidated closing cash with
    unconsolidated flows). Where an identity fails, the involved metrics are replaced
    with the values from a SINGLE source statement whose own figures reconcile it —
    guarded by the identity, so it can only fix, never regress."""
    # Collect primary + note candidates, tagged by tier (0=primary, 1=note), basis, and
    # the index of the source TABLE (so an identity can be reconciled from one statement).
    cand: dict[tuple[str, int], list[tuple[float, int, object, int, str, int]]] = {}
    for ti, t in enumerate(tables):
        role = table_role_of(t)
        if role == "analytical" or not _is_currency_scale(t):
            continue
        # DetailTruthIndex: a note-only truth (the schedules' OWN totals), so detail
        # reconciliation checks leaves against the note total — not the primary face total
        # masquerading as detail. Primary tables are excluded entirely in this mode.
        if notes_only and role != "note":
            continue
        tier = 0 if role == "primary" else 1
        basis = _table_basis(t)
        for li in t.line_items:
            cm = _METRIC_ALIASES.get(li.canonical_metric, li.canonical_metric)
            if not cm:
                continue
            for v in li.values:
                if v.year and v.value is not None:
                    ry = v.source_report_year or (t.source.report_year if t.source else 0) or 0
                    cand.setdefault((cm, v.year), []).append((v.value, ry, v.source or t.source, tier, basis, ti))

    # Per-metric magnitude reference: median of |value|. Anchored to PRIMARY candidates
    # when the metric has any (so note partials don't move it); else fall back to all.
    # Includes zeros — a legitimately-zero metric must not be dropped from the median.
    #
    # CURRENCY-SCALE anchor: a mixed "Analysis of …" table can tag BOTH the real figure
    # (266,748,030) AND its common-size percentage (100) as the same total. The % rows
    # often OUTNUMBER the real ones, which would drag a plain median down to ~100 and make
    # the outlier band reject the real values. So the reference ignores any value more
    # than 1000x below the metric's largest — percentages are ~1e6x smaller than a money
    # figure in thousands, while real year-on-year / consolidated variation stays within ~3x.
    prim_abs: dict[str, list[float]] = {}
    all_abs: dict[str, list[float]] = {}
    for (cm, _y), lst in cand.items():
        for c in lst:
            all_abs.setdefault(cm, []).append(abs(c[0]))
            if c[3] == 0:
                prim_abs.setdefault(cm, []).append(abs(c[0]))

    def _scale_ref(vals: list[float]) -> float:
        nz = [v for v in vals if v]
        if not nz:
            return 0.0
        floor = max(nz) / 1000.0
        return statistics.median([v for v in nz if v >= floor] or nz)

    median_abs = {cm: _scale_ref(prim_abs.get(cm) or vals) for cm, vals in all_abs.items() if vals}

    truth: dict[tuple[str, int], tuple[float, object]] = {}
    for (cm, year), lst in cand.items():
        med = median_abs.get(cm, 0.0)
        # Drop magnitude outliers: an extra-digit OCR error (~10x) must NOT survive,
        # so the band is tight (8x) — well outside normal year-on-year variation but
        # inside a digit slip. Only applied when there's a meaningful median.
        if med > 0:
            plausible = [c for c in lst if abs(c[0]) and med / 8 <= abs(c[0]) <= med * 8]
        else:
            plausible = list(lst)
        chosen = plausible or lst
        # Primary (tier 0) always beats a note fallback (tier 1) for the same pair.
        best_tier = min(c[3] for c in chosen)
        tiered = [c for c in chosen if c[3] == best_tier]
        # Basis preference (standalone template): when the best tier holds an unconsolidated
        # candidate of COMPARABLE magnitude to the others, prefer it over consolidated/generic
        # same-tier values — that's the basis the template targets, and the per-metric median
        # tiebreaker otherwise picks the wrong one (e.g. consolidated equity over unconsolidated).
        # The 2x band is a fragment guard: a mis-tagged unconsolidated SUBTOTAL (~8x below the
        # real total) must NOT win, so it only applies when magnitudes are genuinely close.
        unc = [c for c in tiered if c[4] == prefer_basis]
        if unc and len(unc) < len(tiered):
            ref = max(abs(c[0]) for c in tiered)
            if all(abs(c[0]) * 2 >= ref for c in unc):
                tiered = unc
        # Newest report wins; then prefer the value CLOSEST to the metric's median.
        value, _ry, src, _tier, _basis, _ti = max(tiered, key=lambda c: (c[1], -abs(abs(c[0]) - med)))
        # Normalise expense metrics to the additive-P&L convention (always negative) so
        # face truth, the output formula, and the override all share one sign.
        if cm in _EXPENSE_KEY_METRICS:
            value = -abs(value)
        truth[(cm, year)] = (value, src)
        # Debug trace (only when a list is supplied): record KEY-metric selections that
        # had a genuine choice — multiple candidates or magnitude-outliers dropped — so a
        # wrong pick is localizable from the dump rather than re-derived by hand.
        if trace is not None and cm in KEY_METRICS:
            dropped = [round(c[0], 2) for c in lst if c not in plausible]
            alts = sorted({round(c[0], 2) for c in lst} - {round(value, 2)})
            if dropped or alts:
                trace.append({
                    "kind": "select", "metric": cm, "year": year, "chosen": round(value, 2),
                    "alternatives": alts, "outliers_dropped": dropped, "n_candidates": len(lst),
                })

    # Source-consistent identities: a consolidating group files each statement on two
    # bases and the per-metric pick can mix them, breaking an identity (a consolidated
    # tax with an unconsolidated PBT/PAT; a consolidated closing cash with unconsolidated
    # flows). Where one fails, adopt the values from a single statement that reconciles.
    _reconcile_identities(truth, cand, prefer_basis, trace)
    return truth


_TAX_METRICS = ("tax_expense", "taxation", "income_tax")

# Sum identities used for source-consistent reconciliation: target == sum(terms). A term
# may be a tuple of alternative metric names (first present in a table is used).
_RECONCILE_IDENTITIES = (
    ("cash_at_end_of_period",
     ("cash_at_beginning_of_period", "operating_cash_flow", "investing_cash_flow", "financing_cash_flow")),
    ("profit_after_tax", ("profit_before_tax", _TAX_METRICS)),
    # Balance-sheet structure: pick the totals that actually balance. Resolves same-source
    # conflicts where two candidates exist for a total (e.g. a restated total alongside a
    # stale one) by choosing the set where assets = NCA + CA and assets = equity + liabs.
    ("total_assets", ("non_current_assets", "current_assets")),
    ("total_equity_and_liabilities", ("total_assets",)),
)

_MAX_COMBOS = 256          # bound the per-table candidate search


def _identity_tol(target_val: float) -> float:
    return max(1.0, 0.01 * abs(target_val))


def _norm(metric: str, value: float) -> float:
    """Apply the additive-P&L expense convention so the sum matches stored face truth."""
    return -abs(value) if metric in _EXPENSE_KEY_METRICS else value


def _table_candidates(cand, metric_or_alts, year, ti):
    """All (metric, normalised_value, source) candidates for a term in ONE table/year."""
    metrics = metric_or_alts if isinstance(metric_or_alts, tuple) else (metric_or_alts,)
    out = []
    for m in metrics:
        for c in cand.get((m, year), []):
            if c[5] == ti:
                out.append((m, _norm(m, c[0]), c[2]))
    return out


def _table_combo(cand, target, terms, year, ti):
    """Find a (target, terms) value combo from table `ti` that satisfies target==sum(terms).
    Returns {metric: (value, source)} or None. Bounded by _MAX_COMBOS."""
    import itertools
    import math
    target_cs = _table_candidates(cand, target, year, ti)
    term_cs = [_table_candidates(cand, t, year, ti) for t in terms]
    if not target_cs or any(not tc for tc in term_cs):
        return None
    if len(target_cs) * math.prod(len(tc) for tc in term_cs) > _MAX_COMBOS:
        # Too many combos: collapse each term to its single distinct value if unambiguous.
        term_cs = [tc if len({round(v, 2) for _m, v, _s in tc}) > 1 else tc[:1] for tc in term_cs]
    for tgt in target_cs:
        for combo in itertools.product(*term_cs):
            if abs(sum(v for _m, v, _s in combo) - tgt[1]) <= _identity_tol(tgt[1]):
                picks = {target: (tgt[1], tgt[2])}
                for m, v, s in combo:
                    picks[m] = (v, s)
                return picks
    return None


def _reconcile_identities(truth, cand, prefer_basis: str, trace: list | None = None) -> None:
    """In-place: for each identity/year that currently fails, replace the involved metrics
    with the values from a single source statement that reconciles it. Among reconciling
    statements, prefer the fewest changes to current face truth, then the model's basis.
    Strictly guarded: only adopts on a reconcile, so it can never regress a company."""
    ti_basis = {}
    for lst in cand.values():
        for c in lst:
            ti_basis[c[5]] = c[4]

    for target, terms in _RECONCILE_IDENTITIES:
        years = {y for (cm, y) in truth if cm == target}
        for year in years:
            cur_target = truth.get((target, year))
            cur_terms = [_first_present(truth, t, year) for t in terms]
            if cur_target is None or any(v is None for v in cur_terms):
                continue                                   # identity not fully present
            if abs(sum(cur_terms) - cur_target[0]) <= _identity_tol(cur_target[0]):
                continue                                   # already reconciles
            best = None
            for ti in {c[5] for c in cand.get((target, year), [])}:
                picks = _table_combo(cand, target, terms, year, ti)
                if picks is None:
                    continue
                changes = sum(1 for m, (v, _s) in picks.items()
                              if truth.get((m, year), (None,))[0] != v)
                basis_rank = 0 if ti_basis.get(ti) == prefer_basis else 1
                key = (changes, basis_rank)
                if best is None or key < best[0]:
                    best = (key, picks)
            if best is not None:
                for m, (v, s) in best[1].items():
                    old = truth.get((m, year), (None,))[0]
                    if trace is not None and old != v:
                        trace.append({"kind": "identity_reconcile", "identity": target,
                                      "year": year, "metric": m, "old": old, "new": v})
                    truth[(m, year)] = (v, s)


def _first_present(truth, metric_or_alts, year):
    """Resolve a metric (or tuple of alternatives) from face truth for a year -> value|None."""
    metrics = metric_or_alts if isinstance(metric_or_alts, tuple) else (metric_or_alts,)
    for m in metrics:
        pair = truth.get((m, year))
        if pair is not None:
            return pair[0]
    return None
