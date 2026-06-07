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

import json
import logging

from . import retrieval
from . import insights as insights_mod

_log = logging.getLogger("app.engines.fie")

MAX_STEPS = 6          # tool calls before we force a final answer (latency/cost bound)

_AGENT_SYS = (
    "You are a financial analyst agent answering a question about ONE company's workbook. "
    "Gather evidence by calling tools, then give a final answer. RULES: (1) Never state a "
    "number you did not get from a tool call — the workbook is the only source of truth. "
    "(2) Prefer workbook tools (get_value, get_series, compute_ratio, growth, decompose) for "
    "figures; use insights for qualitative themes and external_search only when the workbook "
    "can't answer. (3) Call list_metrics first if you're unsure which metrics exist. "
    "(4) Keep going until you can answer, then return action='final'. Respond ONLY as JSON "
    "matching the schema: to call a tool use {action:'call', tool, args, thought}; to finish "
    "use {action:'final', answer, findings} where findings is a short list of the key "
    "figures/points you used (each a plain sentence). "
    "Output EXACTLY ONE JSON object and nothing else — no prose, no markdown fences, no "
    "second object."
)

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
    return {"metric": metric, "yoy_growth": yoy}


def _t_decompose(engine, frame, ctx, args):
    total = args.get("total") or "total_assets"
    avail = set(engine.store.available_metrics()) | set(
        engine.store.available_metrics(level="detail"))
    comps = [m for m in _DECOMP.get(total, ()) if m in avail]
    yrs = [y for y in sorted(engine.store.years) if engine._safe_lookup(total, y) is not None]
    if len(yrs) < 2 or not comps:
        return {"total": total, "note": "cannot decompose (need 2 years and known components)"}
    y0, y1 = yrs[-2], yrs[-1]
    facts, movers = [], []
    for m in comps:
        v0, v1 = engine._safe_lookup(m, y0), engine._safe_lookup(m, y1)
        if v0 is None or v1 is None:
            continue
        movers.append({"item": m, "delta": round(v1 - v0, 2), "from": v0, "to": v1})
        for yy in (y0, y1):
            try:
                facts.append(engine.store.lookup(m, yy))
            except KeyError:
                pass
    ctx.evidence += retrieval.evidence_from_facts(engine.store, facts)
    movers.sort(key=lambda d: abs(d["delta"]), reverse=True)
    t0, t1 = engine._safe_lookup(total, y0), engine._safe_lookup(total, y1)
    return {"total": total, "from_year": y0, "to_year": y1,
            "total_delta": (round(t1 - t0, 2) if t0 is not None and t1 is not None else None),
            "movers": movers[:6]}


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


_TOOLS = {
    "list_metrics": _t_list_metrics,
    "get_value": _t_get_value,
    "compute_ratio": _t_compute_ratio,
    "growth": _t_growth,
    "decompose": _t_decompose,
    "insights": _t_insights,
    "external_search": _t_external_search,
}


def run(engine, frame, ctx, *, max_steps: int = MAX_STEPS) -> str | None:
    """Drive the tool-calling loop. Returns the final answer (also stashed on
    ctx.llm_analysis for the response layer to verify + promote)."""
    company = engine._effective_company(frame) or "the company"
    transcript = [
        f"Question: {frame.raw_query}",
        f"Company: {company}",
        f"Available metrics: {sorted(engine.store.available_metrics())}",
        f"Years: {sorted(engine.store.years)}",
    ]
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

    ctx.extra = {**(ctx.extra or {}), "agent_answer": answer,
                 "agent_findings": findings, "agent_steps": steps}
    ctx.llm_analysis = answer  # response layer verifies (numeric guard) + promotes to direct
    return answer
