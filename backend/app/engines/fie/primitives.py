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
    """The 'what's in this workbook' menu the planner selects from. Includes the company's PSX
    ticker + sector, resolved deterministically at load (so sector is a known fact, not guessed
    from a market feed)."""
    s = engine.store
    areas = sorted({(r.get("area") or "").strip() for r in s.insights()} - {""})
    ident = {}
    try:
        from .external import company_identity
        ident = company_identity(engine)
    except Exception:  # noqa: BLE001
        ident = {}
    return {
        "company": s.company,
        "ticker": ident.get("symbol"),
        "sector": ident.get("sector"),
        "years": s.years,
        "sheets": list(s.sheet_names or []),
        "headline_metrics": sorted(s.available_metrics()),
        "detail_metrics": sorted(s.available_metrics(level="detail")),
        "insight_areas": areas,
    }


def availability(engine) -> PrimitiveResult:
    """Workbook-metadata answer (company, sector, year span, sheet/metric counts) packaged as a
    fetched result so it flows through the same COMPOSE+VERIFY pipeline as everything else. The
    counts are registered as calcs so the numeric guard admits '25 sheets / 45 metrics'; the
    `lead` is the deterministic fallback if the composed prose is rejected."""
    s = engine.store
    m = describe_workbook(engine)
    yrs = list(s.years or [])
    span = f"{yrs[0]}–{yrs[-1]}" if len(yrs) > 1 else (str(yrs[0]) if yrs else "no")
    n_sheets = len(m.get("sheets") or [])
    n_metrics = len(m.get("headline_metrics") or [])
    lead = (f"This workbook holds {m.get('company') or 'the company'}"
            + (f" ({m['sector']})" if m.get("sector") else "")
            + f" financials covering {span}, across {n_sheets} sheet(s) with "
            + f"{n_metrics} headline metric(s).")
    res = {"kind": "availability", "lead": lead, "company": m.get("company"),
           "sector": m.get("sector"), "ticker": m.get("ticker"), "year_span": span,
           "n_sheets": n_sheets, "n_metrics": n_metrics, "sheets": m.get("sheets"),
           "metrics": m.get("headline_metrics"), "insight_areas": m.get("insight_areas")}
    calcs = [CalcResult(formula_id="workbook_meta", value=float(n_sheets), confidence="High"),
             CalcResult(formula_id="workbook_meta", value=float(n_metrics), confidence="High")]
    return res, [], calcs


def list_formulas(engine) -> list[dict]:
    """The registry-formula menu (id + definition) — so the LLM maps 'gp margin' -> gross_margin
    rather than guessing arithmetic."""
    reg = engine.calc._registry
    out = []
    for fid, spec in reg.items():
        out.append({
            "id": fid,
            "expression": spec.expression,
            "unit": spec.output_unit,
            "description": (spec.description or spec.category),
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
    if kind == "sector":
        from .external import sector_profitability
        return sector_profitability(engine, need.get("year"))
    if kind == "tool" and need.get("tool"):
        from .tools import run_tool
        return run_tool(engine, need["tool"], need.get("args"))
    hints = getattr(engine, "_hints", None) or {}
    if kind == "api" and (need.get("name") or need.get("index") is not None):
        from .external import call_api, api_for_index
        # planner selects by INDEX; deterministic hints inject by NAME — accept either.
        name = need.get("name") or api_for_index(need.get("index"))
        if not name:
            return {"kind": "api", "note": f"no api for index {need.get('index')}"}, [], []
        # off-workbook asks carry the target company/sector in plan hints — let the API narrow to
        # THAT entity (e.g. Lucky Cement / CEMENT), not always the workbook company.
        return call_api(engine, name, {
            "symbol": need.get("symbol"),
            "company": need.get("company") or hints.get("company") or engine.store.company,
            "query": need.get("query"), "year": need.get("year"),
            "sector": need.get("sector") or hints.get("sector"),
        })
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
    return {"kind": kind, "note": f"unsupported or incomplete need: {need}"}, [], []
