"""Deterministic primitives — the entire rule-base of the engine.

Each primitive is a thin, dumb wrapper over machinery that already exists (the store, the calc
formula registry, the safe expression evaluator). They take the engine + plain args and return
``(result_dict, evidence, calcs)`` where ``result_dict`` is a compact summary for the LLM, and
``evidence``/``calcs`` are the cited objects the numeric guard and citation binder consume.

No primitive ever calls the LLM or makes a routing decision — that is the controller's job. The
LLM only ever *selects* which primitive to run and *writes prose over their outputs*; it never
produces a number itself. That separation is what makes the system safe on a small model.
"""

from __future__ import annotations

import ast
import logging

from . import retrieval
from .calc import registry as calc_registry
from .models import CalcResult, EvidenceItem

_log = logging.getLogger("app.engines.fie")

# Result tuple: (summary dict for the LLM, cited evidence items, calc results)
PrimitiveResult = tuple[dict, list[EvidenceItem], list[CalcResult]]


# --------------------------------------------------------------------------- menus (sent to LLM)
def describe_workbook(engine) -> dict:
    """The 'what's in this workbook' menu the planner selects from (redesign §3/§11).

    `sheets` is a flat {headline-sheet: [canonical metric ids]} map (P&L / Balance Sheet only —
    detail sheets are omitted per D1; their canonical id is just the total, already in headline).
    `years` is split historical vs forecast so forecast years no longer masquerade as data present.
    The ledger/insight blocks carry a one-line `capability` (+ field `schema`) so the planner can
    ROUTE to them — extraction is whole-sheet, so no row data is in the menu."""
    s = engine.store
    ident = {}
    try:
        from .external import company_identity
        ident = company_identity(engine)
    except Exception:  # noqa: BLE001
        ident = {}
    df = s.findata

    def _years(period_type: str) -> list[int]:
        if df is None or df.empty:
            return []
        sel = df[df["period_type"] == period_type]
        return sorted(int(y) for y in sel["year"].dropna().unique())

    # flat {sheet: [canonical metric ids]} — headline, value-bearing, historical only
    sheets: dict[str, list[str]] = {}
    if df is not None and not df.empty:
        sub = df[(df["level"] == "headline") & (df["period_type"] == "historical")
                 & df["value"].notna() & df["metric"].notna()]
        for sheet, grp in sub.groupby("sheet"):
            sheets[str(sheet)] = sorted(set(grp["metric"].dropna().astype(str)))

    ins = s.insights()
    areas = sorted({(r.get("area") or "").strip() for r in ins} - {""})
    ins_years = sorted({r.get("year") for r in ins if isinstance(r.get("year"), int)})

    return {
        "company": s.company,
        "ticker": ident.get("symbol"),
        "sector": ident.get("sector"),
        "years": {"historical": _years("historical"), "forecast": _years("forecast")},
        "sheets": sheets,
        "qualitative_insights": {
            "areas": areas, "years": ins_years,
            "schema": ["area", "takeaway", "year", "source_report_year",
                       "source_section", "page", "confidence"]},
        "edit_history": {
            "capability": "the user's own edits to this workbook — what changed, in which "
                          "sheet/cell, when, saved vs unsaved.",
            "schema": ["timestamp", "sheet", "cell", "old", "new", "saved", "event"]},
        "source_ledger": {
            "capability": "provenance of each figure — which source report / page / table a "
                          "value was taken from.",
            "schema": ["Sheet", "Cell", "Template label", "Matched label", "Year", "Value",
                       "Report year", "Report file", "Page", "Table id", "Confidence", "Note"]},
        "validation_ledger": {
            "capability": "data-quality audit — which metric/year cells are flagged, their "
                          "status, vs face/source truth.",
            "schema": ["Status", "Sheet", "Cell/Label", "Metric", "Year", "Value",
                       "Face truth", "Source", "Note"]},
    }


def availability(engine) -> PrimitiveResult:
    """Workbook-metadata answer (company, sector, year span, sheet/metric counts) packaged as a
    fetched result so it flows through the same COMPOSE+VERIFY pipeline as everything else. The
    counts are registered as calcs so the numeric guard admits '25 sheets / 45 metrics'; the
    `lead` is the deterministic fallback if the composed prose is rejected."""
    s = engine.store
    ident = {}
    try:
        from .external import company_identity
        ident = company_identity(engine)
    except Exception:  # noqa: BLE001
        ident = {}
    yrs = list(s.years or [])
    span = f"{yrs[0]}–{yrs[-1]}" if len(yrs) > 1 else (str(yrs[0]) if yrs else "no")
    # the FULL workbook tab list + headline metric ids (availability speaks of the whole workbook,
    # not the planner's headline-only menu) — self-contained, not derived from describe_workbook.
    sheets = list(s.sheet_names or [])
    metrics = sorted(s.available_metrics())
    areas = sorted({(r.get("area") or "").strip() for r in s.insights()} - {""})
    n_sheets = len(sheets)
    n_metrics = len(metrics)
    lead = (f"This workbook holds {s.company or 'the company'}"
            + (f" ({ident.get('sector')})" if ident.get("sector") else "")
            + f" financials covering {span}, across {n_sheets} sheet(s) with "
            + f"{n_metrics} headline metric(s).")
    res = {"kind": "availability", "lead": lead, "company": s.company,
           "sector": ident.get("sector"), "ticker": ident.get("symbol"), "year_span": span,
           "n_sheets": n_sheets, "n_metrics": n_metrics, "sheets": sheets,
           "metrics": metrics, "insight_areas": areas}
    calcs = [CalcResult(formula_id="workbook_meta", value=float(n_sheets), confidence="High"),
             CalcResult(formula_id="workbook_meta", value=float(n_metrics), confidence="High")]
    return res, [], calcs


# Self-sufficient, unique one-line descriptions (redesign §8) — replace the registry's
# non-discriminating ones ("profitability" for three different margins). With these the menu can
# drop `expression`: the planner selects by id + description, the engine computes from the registry.
_FORMULA_DESC = {
    "revenue_growth": "Year-over-year percentage change in revenue.",
    "earnings_growth": "Year-over-year percentage change in net profit (PAT).",
    "gross_profit_growth": "Year-over-year percentage change in gross profit.",
    "operating_profit_growth": "Year-over-year percentage change in operating profit (EBIT).",
    "pretax_profit_growth": "Year-over-year percentage change in profit before tax.",
    "gross_margin": "Gross profit as a percentage of revenue (revenue after direct/production costs).",
    "operating_margin": "Operating profit (EBIT) as a percentage of revenue.",
    "net_margin": "Net profit (PAT) as a percentage of revenue.",
    "pretax_margin": "Profit before tax as a percentage of revenue.",
    "cogs_ratio": "Cost of goods sold as a percentage of revenue.",
    "opex_ratio": "Operating expenses as a percentage of revenue.",
    "effective_tax_rate": "Income tax expense as a percentage of profit before tax.",
    "roe": "Net profit as a percentage of average shareholders' equity (return on equity).",
    "roa": "Net profit as a percentage of average total assets (return on assets).",
    "return_on_capital_employed": "Operating profit (EBIT) as a percentage of capital employed (ROCE).",
    "equity_ratio": "Share of total assets financed by equity.",
    "current_ratio": "Current assets / current liabilities (short-term liquidity).",
    "quick_ratio": "Liquid current assets excluding inventory / current liabilities.",
    "cash_ratio": "Cash and equivalents / current liabilities.",
    "debt_to_equity": "Total debt relative to total equity (financial leverage).",
    "debt_to_assets": "Share of total assets financed by debt.",
    "long_term_debt_ratio": "Long-term debt as a share of total assets.",
    "equity_multiplier": "Total assets / total equity (leverage).",
    "interest_coverage": "Operating profit (EBIT) relative to interest expense.",
    "asset_turnover": "Revenue generated per unit of average total assets.",
    "fixed_asset_turnover": "Revenue generated per unit of net fixed assets.",
    "inventory_turnover": "Times inventory is sold and replaced (COGS / inventory).",
    "receivables_turnover": "Times receivables are collected (revenue / receivables).",
    "days_sales_outstanding": "Average days to collect receivables (DSO).",
    "days_inventory_outstanding": "Average days inventory is held before sale (DIO).",
    "working_capital": "Current assets minus current liabilities.",
    "capital_employed": "Total assets minus current liabilities (long-term capital in use).",
    "net_debt": "Total debt minus cash and equivalents.",
    "ebitda": "Earnings before interest, tax, depreciation and amortization.",
    "ebitda_margin": "EBITDA as a percentage of revenue.",
    "debt_to_ebitda": "Total debt relative to EBITDA.",
    "free_cash_flow": "Operating cash flow minus capital expenditure.",
    "free_cash_flow_margin": "Free cash flow as a percentage of revenue.",
    "operating_cash_flow_margin": "Operating cash flow as a percentage of revenue.",
    "operating_cash_flow_ratio": "Operating cash flow relative to current liabilities.",
    "cash_flow_to_debt": "Operating cash flow relative to total debt.",
    "capex_to_sales": "Capital expenditure as a percentage of revenue.",
    "payables_turnover": "Speed of paying suppliers (COGS / payables).",
    "days_payable_outstanding": "Average days taken to pay suppliers (DPO).",
    "cash_conversion_cycle": "Days to convert inventory/receivables back to cash (DSO + DIO - DPO).",
    "book_value_per_share": "Common equity per outstanding share.",
    "eps_computed": "Net profit per weighted-average outstanding share (EPS).",
    "dividend_payout_ratio": "Share of net profit paid out as dividends.",
    "retention_ratio": "Share of net profit retained (1 - payout ratio).",
    "forecast_error": "Percentage deviation of actual from forecast.",
}

_UNIT_LABEL = {"percent": "%", "x": "x", "currency": "currency", "days": "days"}


def list_formulas(engine) -> list[dict]:
    """The registry-formula menu (redesign §8): {id, description, unit}. GATED to formulas whose
    inputs all exist in THIS workbook (so the planner can't pick an uncomputable ratio), and with
    `expression` dropped (internal — the engine computes it; the planner selects by id+description)."""
    reg = engine.calc._registry
    avail = engine.store.available_metrics() | engine.store.available_metrics(level="detail")
    out = []
    for fid, spec in reg.items():
        needed = {i.metric for i in spec.inputs if getattr(i, "metric", None)}
        if needed - avail:                       # an input this workbook lacks -> not computable
            continue
        out.append({
            "id": fid,
            "description": _FORMULA_DESC.get(fid, spec.description or spec.category),
            "unit": _UNIT_LABEL.get(str(spec.output_unit), str(spec.output_unit)),
        })
    return out


# ------------------------------------------------------------------------------------ fetch ops
def get_metric(engine, metric: str, year=None) -> PrimitiveResult:
    """Fetch a workbook value (single year) or the whole series (year=None)."""
    s = engine.store
    if year is None:
        series, facts = {}, []
        for y in s.years:
            try:
                f = s.lookup(metric, y)
            except KeyError:
                continue
            if f.value is not None:
                series[y] = f.value
                facts.append(f)
        ev = retrieval.evidence_from_facts(s, facts)
        res = {"metric": metric, "series": series} if series else {"metric": metric, "note": "not found"}
        return res, ev, []
    try:
        f = s.lookup(metric, int(year))
    except (KeyError, ValueError):
        return {"metric": metric, "year": year, "value": None, "note": "not found"}, [], []
    if f.value is None:
        return {"metric": metric, "year": int(year), "value": None, "note": "no value"}, [], []
    return {"metric": metric, "year": int(year), "value": f.value}, retrieval.evidence_from_facts(s, [f]), []


def run_formula(engine, formula_id: str, year) -> PrimitiveResult:
    """Compute a REGISTERED ratio (gross_margin, roe, current_ratio, …) for one year — exact,
    deterministic, guard-passing. Falls through with a note if the formula or inputs are missing."""
    try:
        cr = engine.calc.evaluate(formula_id, int(year))
    except (ValueError, TypeError):
        return {"formula": formula_id, "year": year, "note": "bad year"}, [], []
    except Exception as e:  # noqa: BLE001 — never let a calc hiccup crash the controller
        return {"formula": formula_id, "year": year, "note": f"error: {e}"}, [], []
    if cr is None or cr.value is None:
        note = (cr.note if cr is not None else None) or "could not compute (missing inputs or unknown formula)"
        return {"formula": formula_id, "year": year, "value": None, "note": note}, [], []
    ev = retrieval.evidence_from_facts(engine.store, cr.inputs)
    return {"formula": formula_id, "year": int(year), "value": cr.value, "unit": cr.unit}, ev, [cr]


def compute(engine, expression: str, year=None, label: str | None = None) -> PrimitiveResult:
    """Evaluate an arithmetic EXPRESSION over metric ids (the fallback when no registry formula
    fits). The LLM proposes the expression; the arithmetic is done here, deterministically, over
    workbook values — so the result is citable and passes the numeric guard."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return {"expression": expression, "note": f"parse error: {e}"}, [], []
    names = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id not in ("abs", "min", "max")}
    if not names:
        return {"expression": expression, "note": "no metric identifiers"}, [], []
    years = [int(year)] if year is not None else list(engine.store.years)
    out: dict = {}
    facts: list = []
    calcs: list[CalcResult] = []
    for y in years:
        vals, ok = {}, True
        yr_facts = []
        for nm in names:
            try:
                f = engine.store.lookup(nm, y)
            except KeyError:
                ok = False
                break
            if f.value is None:
                ok = False
                break
            vals[nm] = f.value
            yr_facts.append(f)
        if not ok:
            continue
        try:
            v = round(float(calc_registry.safe_eval(expression, vals)), 6)
        except Exception as e:  # noqa: BLE001
            return {"expression": expression, "note": f"eval error: {e}"}, [], []
        out[y] = v
        facts.extend(yr_facts)
        calcs.append(CalcResult(formula_id="computed", value=v, unit="ratio",
                                inputs=yr_facts, expression=expression, confidence="High"))
    ev = retrieval.evidence_from_facts(engine.store, facts)
    if not out:
        return {"expression": expression, "note": f"missing inputs; needs {sorted(names)}"}, ev, []
    res = {"expression": expression, "label": label,
           "by_year": out, "value": (out.get(int(year)) if year is not None else None)}
    return res, ev, calcs


# Dispatch a single plan "need" to its primitive. Unknown kinds return a note (the controller
# surfaces it as a gap rather than crashing). `frame` is passed for the parity primitives
# (validation/insights/forecast/edit_history) that reuse deterministic tools keyed on the QueryFrame.
def execute_need(engine, need: dict, frame=None) -> PrimitiveResult:
    kind = (need.get("kind") or "").lower()
    if kind == "metric" and need.get("metric"):
        return get_metric(engine, need["metric"], need.get("year"))
    if kind == "formula" and need.get("formula"):
        yr = need.get("year")
        if yr is None:  # a margin with no year -> compute the whole series, newest first
            results: dict = {}
            ev_all, calc_all = [], []
            for y in engine.store.years:
                r, ev, c = run_formula(engine, need["formula"], y)
                if r.get("value") is not None:
                    results[y] = r["value"]
                    ev_all += ev
                    calc_all += c
            if results:
                return {"formula": need["formula"], "by_year": results}, ev_all, calc_all
            return {"formula": need["formula"], "note": "could not compute for any year"}, [], []
        return run_formula(engine, need["formula"], yr)
    if kind == "compute" and need.get("expression"):
        return compute(engine, need["expression"], need.get("year"), need.get("label"))
    if kind == "tool" and need.get("tool"):
        from .tools import run_tool
        return run_tool(engine, need["tool"], need.get("args"))
    hints = getattr(engine, "_hints", None) or {}
    if kind == "news":
        from .external import news_search
        return news_search(engine, need.get("query") or hints.get("company") or engine.store.company)
    if kind == "web":
        from .external import web_search
        return web_search(engine, need.get("query") or "", hints=hints)
    if kind == "validation":
        from .analysis import validate
        return validate(engine, frame)
    if kind == "insights":
        from .analysis import insights as _insights
        return _insights(engine, frame)
    if kind == "forecast":
        from .analysis import project
        return project(engine, frame, {"metric": need.get("metric"),
                                       "to_year": need.get("year"), "growth": need.get("growth")})
    if kind == "edit_history":
        from .analysis import edit_history
        return edit_history(engine, frame, pending_edits=getattr(engine, "_pending_edits", None),
                            now=getattr(engine, "_now", None))
    if kind == "availability":
        return availability(engine)
    if kind == "aggregate":
        # rule-based mean/sum/min/max over LLM-listed values (the composer loop usually computes this
        # with fetched-value validation; this is the plan-time fallback path).
        op = (need.get("op") or "").strip().lower()
        vals = [v for v in (need.get("values") or [])
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if op in ("mean", "sum", "min", "max") and vals:
            res = (sum(vals) / len(vals) if op == "mean" else sum(vals) if op == "sum"
                   else min(vals) if op == "min" else max(vals))
            return ({"kind": "aggregate", "op": op, "label": need.get("label"),
                     "unit": need.get("unit"), "value": res, "components": vals}, [],
                    [CalcResult(formula_id="aggregate", value=float(res), confidence="High")])
        return {"kind": "aggregate", "note": "invalid aggregate need"}, [], []
    return {"kind": kind, "note": f"unsupported or incomplete need: {need}"}, [], []
