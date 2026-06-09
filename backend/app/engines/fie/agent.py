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

MAX_STEPS = 5          # tool calls before we force a final answer (latency/cost bound).
                       # Enough for premise-check → decompose → insights → final; lower keeps
                       # worst-case latency in check on slow models.

# Causal / change phrasing → always pre-run decompose (don't rely on the model to choose it).
_WHY_RE = re.compile(
    r"\bwhy\b|\b(caused?|drove|driven|explain|reason|because)\b|"
    r"\b(less|lower|more|higher|fell|fall|dropp?ed?|declin\w*|ros[e]|grew|grow\w*|"
    r"increas\w*|decreas\w*|chang\w*)\b",
    re.I,
)

_AGENT_SYS = (
    "You are a financial analyst agent answering a question about ONE company's workbook. "
    "Gather evidence by calling tools, then give a final answer. RULES: "
    "(1) Never state a number you did not get from a tool call — the workbook is the only "
    "source of truth. "
    "(2) PREMISE CHECK — if the question asserts a comparison or direction (e.g. 'why was X "
    "less than Y', 'why did X fall', 'why is X higher in 2024'), FIRST verify it with "
    "get_value/growth/decompose. If the data contradicts the premise, say so plainly and "
    "correct it BEFORE explaining anything else. "
    "(3) WHY / CAUSAL — to explain why a metric changed, call decompose(metric, from_year, "
    "to_year): it attributes the change to that metric's components and works for ratios AND "
    "statement aggregates (e.g. gross_profit, operating_profit, pat, total_assets). Then, for "
    "the underlying business reason, call insights (and external_search only if the workbook "
    "can't answer). "
    "(4) Prefer workbook tools for figures: get_value, growth, decompose; compute_ratio for a "
    "REGISTERED ratio, else compute_expr to evaluate any arithmetic over metric ids (e.g. ROIC = "
    "'operating_profit / (total_assets - current_liabilities)'); find_line_item to locate a line "
    "item not in the headline set; list_metrics if unsure what exists. "
    "(4a) For AUDIT / VALIDATION questions ('does the balance sheet balance', 'do the numbers add "
    "up', 'find anomalies', 'sum the components vs the total'), call check_balance (identities + "
    "component footing) and/or scan_anomalies (figures that look mis-extracted), then report the "
    "breaks plainly. "
    "(4b) For MARKET / VALUATION questions (share price, P/E, market cap, dividend yield, 'is it "
    "undervalued'), call market_data — prefer its REPORTED P/E; combine its price with workbook "
    "fundamentals (EPS, equity, dividends) via compute_expr for yield / EV / P/B. "
    "(4c) For FORWARD PROJECTION questions ('project/estimate revenue for 2026', 'what will X be "
    "next year'), call project. ALWAYS present the result as a SCENARIO under the stated growth "
    "assumption and give the low/base/high range — NEVER state a projection as a fact or "
    "certainty. "
    "(5) Keep going until you can answer, then return action='final'. Respond ONLY as JSON "
    "matching the schema: to call a tool use {action:'call', tool, args, thought}; to finish "
    "use {action:'final', answer, findings} where findings is a short list of the key "
    "figures/points you used (each a plain sentence). "
    "Output EXACTLY ONE JSON object and nothing else — no prose, no markdown fences, no "
    "second object."
)

# A second model re-checks the draft answer against the raw tool outputs (ground truth) and
# rewrites it if a number is unsupported or a premise wasn't verified. The downstream numeric
# guard (safety.verify_prose) still applies, so any number must also be in the evidence.
_VERIFY_SYS = (
    "You are a fact-checker for a financial analyst agent. You are given the user's question, "
    "the agent's draft answer, and the RAW tool outputs the agent collected (the only ground "
    "truth). Check: (a) every number in the answer appears in the tool outputs; (b) if the "
    "question asserts a premise (e.g. 'why was X less than Y'), the answer actually verified it "
    "and corrected it if the data disagrees; (c) the explanation follows from the tool outputs. "
    "Reply with ONE JSON object {supported: bool, premise_ok: bool, issues: [string], "
    "revised_answer: string|null}. If anything is wrong, put a corrected answer grounded ONLY in "
    "the tool outputs in revised_answer; otherwise revised_answer = null. No prose, no fences."
)
_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "premise_ok": {"type": "boolean"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "revised_answer": {"type": ["string", "null"]},
    },
    "required": ["supported"],
}

_AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["call", "final"]},
        "tool": {"type": ["string", "null"]},
        "args": {"type": ["object", "null"]},
        "answer": {"type": ["string", "null"]},
        "findings": {"type": ["array", "null"], "items": {"type": "string"}},
    },
    "required": ["action"],
}

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


def _two_years(engine, target, args):
    """Resolve the (earlier, later) year pair to compare: explicit from/to, a single year (vs
    prior), or the last two years that carry data for ``target``. Returns (None, None) if <2."""
    fy, ty = args.get("from_year"), args.get("to_year")
    try:
        if fy is not None and ty is not None:
            a, b = int(fy), int(ty)
            return (a, b) if a <= b else (b, a)
        if args.get("year") is not None:
            y = int(args["year"])
            return y - 1, y
    except (TypeError, ValueError):
        pass
    have = [y for y in sorted(engine.store.years) if engine._safe_lookup(target, y) is not None]
    return (have[-2], have[-1]) if len(have) >= 2 else (None, None)


# --- tools: each takes (engine, frame, ctx, args) -> compact observation dict --------------
def _t_list_metrics(engine, frame, ctx, args):
    return {"metrics": sorted(engine.store.available_metrics()),
            "years": sorted(engine.store.years)}


def _t_get_value(engine, frame, ctx, args):
    metric = args.get("metric")
    if not metric:
        return {"error": "metric is required"}
    year = args.get("year")
    if year is None:  # whole series
        series, facts = {}, []
        for y in sorted(engine.store.years):
            v = engine._safe_lookup(metric, y)
            if v is not None:
                series[y] = v
                try:
                    facts.append(engine.store.lookup(metric, y))
                except KeyError:
                    pass
        ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
        return {"metric": metric, "series": series} if series else {"metric": metric, "note": "no values"}
    v = engine._safe_lookup(metric, int(year))
    if v is None:
        return {"metric": metric, "year": year, "value": None, "note": "not found"}
    try:
        ctx.evidence += retrieval.evidence_from_facts(engine.store, [engine.store.lookup(metric, int(year))])
    except KeyError:
        pass
    return {"metric": metric, "year": int(year), "value": v}


def _t_compute_ratio(engine, frame, ctx, args):
    formula = args.get("formula")
    if not formula:
        return {"error": "formula is required (e.g. roe, current_ratio, net_margin)"}
    year = args.get("year")
    years = [int(year)] if year is not None else sorted(engine.store.years)
    out = {}
    for y in years:
        try:
            cr = engine.calc.evaluate(formula, y)
        except Exception:  # noqa: BLE001
            cr = None
        if cr is not None and cr.value is not None:
            out[y] = cr.value
            ctx.calcs.append(cr)
            ctx.evidence += retrieval.evidence_from_facts(engine.store, cr.inputs)
    if not out:
        return {"formula": formula, "note": "could not compute (missing inputs or unknown formula)"}
    return {"formula": formula, "by_year": out}


def _t_growth(engine, frame, ctx, args):
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
    yoy = {series[i][0]: round(series[i][1] / series[i - 1][1] - 1, 4)
           for i in range(1, len(series)) if series[i - 1][1]}
    _allow(ctx, *yoy.values())  # growth rates are derived from cited values → allow in prose
    return {"metric": metric, "yoy_growth": yoy}


def _t_decompose(engine, frame, ctx, args):
    """Generic change-attribution for ANY metric between two years:
      • statement aggregate (gross_profit/operating_profit/pat/total_assets/…) → signed
        additive component contributions, ranked;
      • ratio/formula (roe, net_margin, current_ratio, …) → the input metrics that moved;
      • base line item (revenue, a single cost) → just the change (no sub-components).
    Every figure used is added to ctx.evidence so the numeric guard accepts the answer."""
    target = args.get("metric") or args.get("total") or "total_assets"
    y0, y1 = _two_years(engine, target, args)
    if y0 is None:
        return {"metric": target, "note": "need at least two years of data to decompose"}
    avail = set(engine.store.available_metrics()) | set(
        engine.store.available_metrics(level="detail"))
    facts: list = []

    def _add(metric, *years):
        for yy in years:
            try:
                facts.append(engine.store.lookup(metric, yy))
            except KeyError:
                pass

    # 1) statement aggregate → signed additive attribution
    if target in _STATEMENT_DECOMP:
        movers = []
        for m, sign in _STATEMENT_DECOMP[target]:
            if m not in avail:
                continue
            v0, v1 = engine._safe_lookup(m, y0), engine._safe_lookup(m, y1)
            if v0 is None or v1 is None:
                continue
            movers.append({"item": m, "from": v0, "to": v1, "delta": round(v1 - v0, 2),
                           "contribution": round(sign * (v1 - v0), 2)})
            _add(m, y0, y1)
        ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
        movers.sort(key=lambda d: abs(d["contribution"]), reverse=True)
        t0, t1 = engine._safe_lookup(target, y0), engine._safe_lookup(target, y1)
        td = round(t1 - t0, 2) if t0 is not None and t1 is not None else None
        _allow(ctx, td, *[m["delta"] for m in movers], *[m["contribution"] for m in movers])
        return {"kind": "aggregate", "metric": target, "from_year": y0, "to_year": y1,
                "from": t0, "to": t1, "total_delta": td, "movers": movers[:8]}

    # 2) ratio / formula → attribute to the inputs that moved
    spec = calc_registry.get(target)
    if spec is not None:
        seen, inputs = set(), []
        for inp in spec.inputs:
            if inp.metric in seen:
                continue
            seen.add(inp.metric)
            v0, v1 = engine._safe_lookup(inp.metric, y0), engine._safe_lookup(inp.metric, y1)
            if v0 is None or v1 is None:
                continue
            inputs.append({"input": inp.metric, "from": v0, "to": v1, "delta": round(v1 - v0, 2),
                           "pct_change": (round(v1 / v0 - 1, 4) if v0 else None)})
            _add(inp.metric, y0, y1)
        ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
        rv = {}
        for yy in (y0, y1):
            try:
                cr = engine.calc.evaluate(target, yy)
            except Exception:  # noqa: BLE001
                cr = None
            if cr is not None and cr.value is not None:
                rv[yy] = cr.value
                ctx.calcs.append(cr)
        _allow(ctx, *[i["delta"] for i in inputs], *[i["pct_change"] for i in inputs])
        return {"kind": "ratio", "metric": target, "from_year": y0, "to_year": y1,
                "ratio_from": rv.get(y0), "ratio_to": rv.get(y1), "inputs": inputs}

    # 3) base line item → no sub-components; report the change so the agent can state it
    v0, v1 = engine._safe_lookup(target, y0), engine._safe_lookup(target, y1)
    if v0 is None or v1 is None:
        return {"metric": target, "note": "not a decomposable metric and values are missing"}
    _add(target, y0, y1)
    ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
    _allow(ctx, round(v1 - v0, 2), (round(v1 / v0 - 1, 4) if v0 else None))
    return {"kind": "change", "metric": target, "from_year": y0, "to_year": y1,
            "from": v0, "to": v1, "delta": round(v1 - v0, 2),
            "pct_change": (round(v1 / v0 - 1, 4) if v0 else None),
            "note": "base line item — no sub-components; use insights for the underlying reason"}


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
    user_g = args.get("growth")
    if user_g is not None:
        try:
            base_g = float(user_g)
            if abs(base_g) > 1:           # accept "10" as 10%
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


def _t_market_data(engine, frame, ctx, args):
    """Live market data — share price / P-E / market cap / shares — from the PSX company
    overview, for valuation questions ('what's the P/E', 'is it undervalued', dividend yield,
    market cap). Adds the figures as CITED external evidence (so they're usable in the answer).
    Prefer the reported P/E here; for yield/EV combine with workbook fundamentals."""
    company = args.get("company") or getattr(frame, "company", None)
    try:
        ticker = engine._ticker(company)
    except Exception:  # noqa: BLE001
        ticker = None
    if not ticker:
        return {"note": "could not resolve a stock ticker for this company"}
    if getattr(engine, "external", None) is None or engine.external.company_overview is None:
        return {"note": "market-data source not configured"}
    try:
        md = engine._market_data(ticker, ctx)
    except Exception as exc:  # noqa: BLE001 — market data is best-effort
        return {"note": f"market-data fetch failed: {exc}"}
    fields = {k: v for k, v in md.items() if not k.startswith("_") and v is not None}
    return {"ticker": ticker, **fields} if fields else {"ticker": ticker, "note": "no market data returned"}


def _t_external_search(engine, frame, ctx, args):
    before = len(ctx.evidence)
    try:
        engine._external_fallback(frame, ctx)
    except Exception as exc:  # noqa: BLE001 — external is best-effort
        return {"note": f"external search failed: {exc}"}
    added = ctx.evidence[before:]
    return {"external_items": [(e.citations[0].locator.get("source") if e.citations else "external",
                                (e.claim or "")[:100]) for e in added[:8]],
            "count": len(added)}


# ── general calculator (gap: ad-hoc / unregistered ratios) ─────────────────────
def _t_compute_expr(engine, frame, ctx, args):
    """General calculator: evaluate an arithmetic expression whose identifiers are metric ids,
    e.g. 'operating_profit / (total_assets - current_liabilities)' (ROIC). Resolves each metric
    from the workbook; supports + - * / ** and abs/min/max. The result is registered with the
    numeric guard, so figures the registry has no formula for (ROIC, FCF, per-share math, …)
    can still be computed and stated."""
    expr = args.get("expression") or args.get("formula")
    if not expr:
        return {"error": "expression required, e.g. 'operating_profit / (total_assets - current_liabilities)'"}
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        return {"error": f"could not parse expression: {e}"}
    names = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and n.id not in ("abs", "min", "max")}
    if not names:
        return {"error": "expression has no metric identifiers"}
    year = args.get("year")
    years = [int(year)] if year is not None else sorted(engine.store.years)
    out: dict = {}
    facts: list = []
    for y in years:
        values, missing = {}, []
        for nm in names:
            v = engine._safe_lookup(nm, y)
            if v is None:
                missing.append(nm)
                continue
            values[nm] = v
            try:
                facts.append(engine.store.lookup(nm, y))
            except KeyError:
                pass
        if missing:
            continue  # can't evaluate this year — an input is absent
        try:
            out[y] = round(float(calc_registry.safe_eval(expr, values)), 6)
        except Exception as e:  # noqa: BLE001
            return {"expression": expr, "error": f"evaluation failed: {e}"}
    ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
    _allow(ctx, *out.values())
    if not out:
        return {"expression": expr, "note": f"missing inputs; metrics needed: {sorted(names)}"}
    return ({"expression": expr, "by_year": out} if year is None
            else {"expression": expr, "year": int(year), "value": out.get(int(year))})


# ── ontology: fuzzy line-item lookup (gap: items outside the canonical set) ─────
def _t_find_line_item(engine, frame, ctx, args):
    """Fuzzy-find an extracted line item by label across BOTH headline and detail metrics —
    for questions about items not in the canonical headline set. Returns ranked metric ids the
    agent can then get_value/decompose. Bounded by what extraction actually captured."""
    q = (args.get("query") or args.get("label") or "").strip().lower()
    if not q:
        return {"error": "query/label is required"}
    cands = set(engine.store.available_metrics()) | set(
        engine.store.available_metrics(level="detail"))
    toks = set(q.replace("_", " ").split())
    scored = []
    for m in cands:
        mt = set(m.replace("_", " ").lower().split())
        overlap = len(toks & mt)
        if overlap:
            scored.append((overlap / max(len(toks), 1), m))
    scored.sort(reverse=True)
    matches = [m for _s, m in scored[:8]]
    return {"query": q, "matches": matches,
            "note": ("use get_value/decompose on a match" if matches
                     else "no matching line item was extracted from this workbook")}


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


_TOOLS = {
    "list_metrics": _t_list_metrics,
    "get_value": _t_get_value,
    "compute_ratio": _t_compute_ratio,
    "compute_expr": _t_compute_expr,
    "find_line_item": _t_find_line_item,
    "growth": _t_growth,
    "decompose": _t_decompose,
    "check_balance": _t_check_balance,
    "scan_anomalies": _t_scan_anomalies,
    "project": _t_project,
    "market_data": _t_market_data,
    "insights": _t_insights,
    "external_search": _t_external_search,
}


def run(engine, frame, ctx, *, max_steps: int = MAX_STEPS, verify: bool = True) -> str | None:
    """Drive the tool-calling loop. Returns the final answer (also stashed on
    ctx.llm_analysis for the response layer to verify + promote). When ``verify`` is on, a
    second model re-checks the answer against the tool outputs and may rewrite it."""
    company = engine._effective_company(frame) or "the company"
    transcript = [
        f"Question: {frame.raw_query}",
        f"Company: {company}",
        f"Available metrics: {sorted(engine.store.available_metrics())}",
        f"Years: {sorted(engine.store.years)}",
    ]

    # ALWAYS decompose for a causal "why did <metric> change" question — pre-run it here rather
    # than relying on the model to pick the tool (small models shortcut to a bare lookup). It's
    # deterministic (no LLM call, no step consumed), registers its derived figures with the
    # numeric guard, and seeds the transcript so the answer explains WHAT drove the change.
    primary = next(iter(frame.metrics or []), None)
    if (primary and _WHY_RE.search(frame.raw_query or "")
            and (primary in _STATEMENT_DECOMP or calc_registry.get(primary) is not None)):
        dargs: dict = {"metric": primary}
        if getattr(frame, "year", None):
            dargs["year"] = frame.year
        try:
            dobs = _t_decompose(engine, frame, ctx, dargs)
            transcript.append(f"Tool decompose args={json.dumps(dargs)} -> "
                              f"{json.dumps(dobs, default=str)[:1500]}")
            transcript.append(
                "Use this decomposition to explain WHAT drove the change. First check the "
                "question's premise against these numbers — if it's false (e.g. the figure rose "
                "when the question assumes it fell), say so plainly, then explain the real movement."
            )
            _log.info("fie agent: pre-decomposed %r for causal query", primary,
                      extra={"component": "Agent"})
        except Exception as exc:  # noqa: BLE001 — pre-decompose is best-effort
            _log.warning("fie agent: pre-decompose failed: %s", exc, extra={"component": "Agent"})

        # Pre-seed qualitative insights too (same reasoning): the arithmetic shows WHAT moved;
        # the management-commentary themes give WHY. Small models often skip the insights tool,
        # so seed it deterministically. If the MD&A had no relevant theme extracted, this is a
        # no-op and the answer stays at the (correct) arithmetic level — no downgrade.
        try:
            iobs = _t_insights(engine, frame, ctx, {})
            if (iobs.get("insights") or []):
                transcript.append(f"Tool insights -> {json.dumps(iobs, default=str)[:1200]}")
                transcript.append(
                    "If any theme above plausibly explains the driver that moved, cite it as the "
                    "likely REASON (e.g. input-cost inflation behind a cost-of-sales rise). Do not "
                    "invent a reason the themes don't support — the numbers alone are still a valid answer."
                )
                _log.info("fie agent: pre-seeded %d insight(s) for causal query",
                          len(iobs.get("insights") or []), extra={"component": "Agent"})
        except Exception as exc:  # noqa: BLE001 — insight pre-seed is best-effort
            _log.warning("fie agent: pre-seed insights failed: %s", exc, extra={"component": "Agent"})

    answer, findings, steps = None, [], 0
    for steps in range(1, max_steps + 1):
        # Retry once on a malformed/non-dict response (the model occasionally emits trailing
        # text or a second object → JSONDecodeError) before giving up — a transient parse
        # failure shouldn't abort the whole run with zero tool calls.
        data = engine.llm.complete_json(_AGENT_SYS, "\n".join(transcript), _AGENT_SCHEMA)
        if not isinstance(data, dict):
            data = engine.llm.complete_json(
                _AGENT_SYS, "\n".join(transcript)
                + "\n\n(Reply with ONE valid JSON object only.)", _AGENT_SCHEMA)
        if not isinstance(data, dict):
            _log.warning("fie agent: LLM returned non-dict at step %d (after retry) -> stop",
                         steps, extra={"component": "Agent"})
            break
        if data.get("action") == "final":
            answer = (data.get("answer") or "").strip() or None
            findings = data.get("findings") or []
            _log.info("fie agent: final after %d step(s); findings=%d", steps, len(findings),
                      extra={"component": "Agent"})
            break
        tool = data.get("tool")
        targs = data.get("args") or {}
        fn = _TOOLS.get(tool)
        if fn is None:
            obs = {"error": f"unknown tool {tool!r}; available: {sorted(_TOOLS)}"}
        else:
            try:
                obs = fn(engine, frame, ctx, targs)
            except Exception as exc:  # noqa: BLE001 — a tool error shouldn't crash the run
                obs = {"error": f"{tool} failed: {exc}"}
        _log.debug("fie agent step %d: %s(%s) -> %s", steps, tool, targs,
                   json.dumps(obs, default=str)[:300], extra={"component": "Agent"})
        transcript.append(f"Tool {tool} args={json.dumps(targs, default=str)} -> "
                          f"{json.dumps(obs, default=str)[:1500]}")
    else:
        _log.info("fie agent: hit max_steps=%d without final", max_steps,
                  extra={"component": "Agent"})

    # Verification pass — re-check the draft against the raw tool outputs (the ground truth),
    # confirm any premise was verified, and rewrite the answer if it isn't fully supported.
    # transcript[4:] is the tool-result log (the first 4 lines are the question/context header).
    verification = None
    if answer and verify:
        vdata = engine.llm.complete_json(
            _VERIFY_SYS,
            f"Question: {frame.raw_query}\n\nAgent answer:\n{answer}\n\n"
            "Tool outputs (ground truth):\n" + "\n".join(transcript[4:]),
            _VERIFY_SCHEMA,
        )
        if isinstance(vdata, dict):
            verification = {k: vdata.get(k) for k in ("supported", "premise_ok", "issues")}
            revised = (vdata.get("revised_answer") or "").strip()
            if revised and (vdata.get("supported") is False or vdata.get("premise_ok") is False):
                _log.info("fie agent: verification rewrote the answer (supported=%s premise_ok=%s)",
                          vdata.get("supported"), vdata.get("premise_ok"),
                          extra={"component": "Agent"})
                answer = revised

    ctx.extra = {**(ctx.extra or {}), "agent_answer": answer, "agent_findings": findings,
                 "agent_steps": steps, "agent_verification": verification}
    ctx.llm_analysis = answer  # response layer verifies (numeric guard) + promotes to direct
    return answer
