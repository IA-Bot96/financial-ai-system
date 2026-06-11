"""Agentic planner (L2.5) — an LLM that composes DETERMINISTIC tools.

The deterministic intents (metric_lookup / ratio / trend / overview / comparison / driver /
forecast / risk) each answer one query *shape*. Anything that doesn't fit a shape — a novel
or multi-step analyst question — used to dead-end. This module lets the LLM act as a planner:
it calls a fixed set of tools that read/compute over the workbook (and optionally fetch
external sources), each returning data PLUS cited EvidenceItems. The LLM decides *which*
tools to call and how to combine them, but every number still originates from a deterministic
tool — so citations and the numeric-safety guard (safety.verify_prose) remain intact.

The final answer is stashed on ``ctx.llm_analysis`` and promoted to the direct answer by the
response layer only if it passes the numeric guard.
"""

from __future__ import annotations

import ast
import json
import logging
import re

from . import retrieval
from . import insights as insights_mod
from .calc import registry as calc_registry
from .models import CalcResult

_log = logging.getLogger("app.engines.fie")


# Asset / liability+equity component sets for the decompose tool (mirrors the driver intent).
_DECOMP = {
    "total_assets": ("property_plant_equipment", "operating_fixed_assets",
                     "capital_work_in_progress", "intangible_assets", "investment_property",
                     "long_term_investments", "long_term_deposits", "right_of_use_assets",
                     "stock_in_trade", "stores_spares_loose_tools", "trade_debts",
                     "loans_and_advances", "deposits_prepayments_other_receivables",
                     "cash_and_bank"),
    "total_equity_and_liabilities": ("paid_up_capital", "reserves", "capital_reserves",
                                     "revenue_reserves", "unappropriated_profit",
                                     "lease_liabilities", "deferred_tax_liability",
                                     "trade_payables", "short_term_borrowings",
                                     "creditors_accrued_other_liabilities",
                                     "accrued_liabilities", "contract_liabilities"),
}

# Signed additive build-ups for statement aggregates: a change in the aggregate is the sum of
# its components' changes (sign = how the component feeds the aggregate). This is the fixed
# income-statement waterfall + balance-sheet identity — finite structure, not per-query code.
# Combined with the calc-engine formula graph (ratios), decompose covers essentially any metric.
_STATEMENT_DECOMP = {
    "gross_profit": (("revenue", 1), ("cost_of_sales", -1)),
    "operating_profit": (("gross_profit", 1), ("administrative_expenses", -1),
                         ("distribution_marketing_expenses", -1),
                         ("other_income", 1), ("other_expenses", -1)),
    "pat": (("operating_profit", 1), ("finance_cost", -1),
            ("other_income", 1), ("taxation", -1)),
    "total_assets": tuple((m, 1) for m in _DECOMP["total_assets"]),
    "total_equity_and_liabilities": tuple((m, 1) for m in _DECOMP["total_equity_and_liabilities"]),
}


def _allow(ctx, *values):
    """Register agent-DERIVED figures (deltas, contributions, growth rates) with the numeric
    guard. They are exact arithmetic over already-cited facts, so the final answer is allowed to
    state them — this mirrors how compute_ratio's CalcResults are whitelisted by
    safety.build_allowed. Appended (not prepended) so a real computed ratio stays calcs[0]."""
    for v in values:
        if v is None:
            continue
        try:
            ctx.calcs.append(CalcResult(formula_id="agent_derived", value=float(v),
                                        confidence="High"))
        except Exception:  # noqa: BLE001
            pass


def _t_insights(engine, frame, ctx, args):
    all_ins = engine.store.insights(include_review=True)
    sel, res = engine.insights.select_and_resolve(frame, all_ins)
    if sel:
        ctx.selected_insights = sel
        ctx.insight_resolutions = res
        ctx.total_insights = len(all_ins)
        ctx.evidence += [insights_mod.insight_evidence(r) for r in sel]
    return {"insights": [{"area": r.get("area"), "takeaway": (r.get("takeaway") or "")[:160]}
                         for r in (sel or [])[:8]]}


def _pstdev(xs: list[float]):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def _t_project(engine, frame, ctx, args):
    """Forward PROJECTION — a SCENARIO, never a forecast of fact. Projects a metric from the
    latest actual using an explicit growth assumption (a user-given rate if provided, else the
    historical CAGR) and returns a low/base/high band (base ± historical YoY volatility) so it's
    never a single point estimate. Historical values are cited; the projected figures are
    registered so the answer may state them — but the answer MUST be framed as an
    assumption-based scenario (see the disclaimer)."""
    metric = args.get("metric")
    if not metric:
        return {"error": "metric is required"}
    series, facts = [], []
    for y in sorted(engine.store.years):
        v = engine._safe_lookup(metric, y)
        if v is not None:
            series.append((y, v))
            try:
                facts.append(engine.store.lookup(metric, y))
            except KeyError:
                pass
    ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
    if len(series) < 2:
        return {"metric": metric, "note": "need at least two historical years to project"}
    (y0, v0), (yN, vN) = series[0], series[-1]
    n = yN - y0
    cagr = (vN / v0) ** (1 / n) - 1 if (v0 > 0 and vN > 0 and n > 0) else None
    yoys = [series[i][1] / series[i - 1][1] - 1 for i in range(1, len(series)) if series[i - 1][1]]
    avg_yoy = sum(yoys) / len(yoys) if yoys else None
    # Accept the user's growth rate under any of the names a model might choose (it is NOT
    # always "growth") — otherwise the explicit assumption is silently dropped and the tool
    # falls back to the historical CAGR (e.g. a "10% growth" request projected at 4.35%).
    user_g = next((args[k] for k in ("growth", "growth_assumption", "growth_rate", "growth_pct",
                                     "growth_percent", "rate", "pct", "assumed_growth")
                   if args.get(k) is not None), None)
    if user_g is not None:
        try:
            base_g = float(str(user_g).strip().rstrip("%").strip())   # tolerate "10", "10%", 0.1
            if abs(base_g) > 1:           # accept "10" / "10%" as 10%
                base_g /= 100.0
        except (TypeError, ValueError):
            base_g = None
    else:
        base_g = cagr if cagr is not None else avg_yoy
    if base_g is None:
        return {"metric": metric, "note": "could not derive a growth assumption"}
    to_year = int(args.get("to_year") or (yN + 1))
    horizon = max(1, to_year - yN)
    spread = max(0.02, _pstdev(yoys) or 0.03)
    scen, proj = {}, []
    for label, g in (("low", base_g - spread), ("base", base_g), ("high", base_g + spread)):
        val = round(vN * ((1 + g) ** horizon), 2)
        scen[label] = {"growth": round(g, 4), "value": val}
        proj += [val, round(g, 4)]
    _allow(ctx, *proj, round(base_g, 4), vN,
           *( [round(cagr, 4)] if cagr is not None else [] ),
           *( [round(avg_yoy, 4)] if avg_yoy is not None else [] ))
    return {"metric": metric, "scenario": True, "from_year": yN, "from_value": vN,
            "to_year": to_year, "horizon_years": horizon,
            "assumption": ("user-specified growth" if user_g is not None else "historical CAGR"),
            "history_cagr": (round(cagr, 4) if cagr is not None else None),
            "avg_yoy": (round(avg_yoy, 4) if avg_yoy is not None else None),
            "projection": scen,
            "disclaimer": "This is a SCENARIO under the stated growth assumption — not a forecast "
                          "of fact. Present it as an assumption-based estimate with the range."}


# ── financial-data validation / audit (gap: blind trust in extraction) ─────────
# Accounting identities (sign-safe: balance-sheet totals share one convention). Each entry:
# (label, [lhs metric ids], [rhs metric ids]) — the LHS sum should equal the RHS sum.
_IDENTITIES = [
    ("assets equal equity + liabilities", ["total_assets"], ["total_equity_and_liabilities"]),
    ("current + non-current assets equal total assets",
     ["current_assets", "non_current_assets"], ["total_assets"]),
    ("liabilities + equity equal total equity & liabilities",
     ["current_liabilities", "non_current_liabilities", "total_equity"],
     ["total_equity_and_liabilities"]),
]


def _foots(a: float, b: float, *, rel: float = 0.01, abs_floor: float = 1.0) -> bool:
    """Equal within 1% relative, or a tiny absolute floor (handles near-zero)."""
    return abs(a - b) <= max(rel * max(abs(a), abs(b)), abs_floor)


def _t_check_balance(engine, frame, ctx, args):
    """Audit the statements deterministically, every year: (1) accounting identities hold, and
    (2) component line items foot to their stated totals. Surfaces breaks like 'assets don't
    equal equity + liabilities' or 'asset components don't sum to the stated total'. Components
    are summed AS STORED (natural signs), so it works whether expenses are stored +/-."""
    years = sorted(engine.store.years)
    facts: list = []
    checks: list = []

    def _vals(metrics, y):
        out = []
        for m in metrics:
            v = engine._safe_lookup(m, y)
            if v is None:
                return None
            out.append(v)
            try:
                facts.append(engine.store.lookup(m, y))
            except KeyError:
                pass
        return out

    for label, lhs, rhs in _IDENTITIES:
        for y in years:
            lv, rv = _vals(lhs, y), _vals(rhs, y)
            if lv is None or rv is None:
                continue
            L, R = round(sum(lv), 2), round(sum(rv), 2)
            checks.append({"check": label, "year": y, "lhs": L, "rhs": R,
                           "variance": round(L - R, 2), "ok": _foots(L, R)})
            _allow(ctx, L, R, round(L - R, 2))

    avail = set(engine.store.available_metrics()) | set(
        engine.store.available_metrics(level="detail"))
    for total in (args.get("totals") or list(_STATEMENT_DECOMP.keys())):
        comps = [m for (m, _s) in _STATEMENT_DECOMP.get(total, ()) if m in avail]
        if not comps:
            continue
        for y in years:
            t = engine._safe_lookup(total, y)
            if t is None:
                continue
            present = [v for v in (engine._safe_lookup(m, y) for m in comps) if v is not None]
            if len(present) < 2:
                continue
            s = round(sum(present), 2)
            checks.append({"check": f"{total} components foot to total", "year": y,
                           "summed": s, "stated": round(t, 2),
                           "variance": round(s - t, 2), "ok": _foots(s, t)})
            _allow(ctx, s, round(t, 2), round(s - t, 2))
            _vals([total], y)

    ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
    breaks = [c for c in checks if not c["ok"]]
    return {"checks_run": len(checks), "breaks": breaks[:12], "all_ok": not breaks}


def _t_scan_anomalies(engine, frame, ctx, args):
    """Flag figures that deviate sharply from their own year-over-year series (a likely
    extraction artifact — e.g. one year ~2x its neighbours). Magnitude-based, so sign-agnostic.
    Catches the kind of mis-extraction the engine would otherwise reason over confidently."""
    metric = args.get("metric")
    metrics = [metric] if metric else sorted(engine.store.available_metrics())
    years = sorted(engine.store.years)
    flags, facts = [], []
    for m in metrics:
        series = [(y, engine._safe_lookup(m, y)) for y in years]
        series = [(y, v) for (y, v) in series if v is not None]
        if len(series) < 3:
            continue
        for i, (y, v) in enumerate(series):
            neighbours = sorted(abs(vv) for j, (_yy, vv) in enumerate(series) if j != i)
            med = neighbours[len(neighbours) // 2]
            if med and abs(v) > 2.0 * med:
                flags.append({"metric": m, "year": y, "value": round(v, 2),
                              "neighbour_median": round(med, 2), "ratio": round(abs(v) / med, 2)})
                try:
                    facts.append(engine.store.lookup(m, y))
                except KeyError:
                    pass
    flags.sort(key=lambda f: f["ratio"], reverse=True)
    ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
    _allow(ctx, *[f["value"] for f in flags])
    return {"anomalies": flags[:15], "metrics_scanned": len(metrics)}
