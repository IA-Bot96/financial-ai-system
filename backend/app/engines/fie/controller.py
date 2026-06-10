"""The engine controller: PLAN -> FETCH -> COMPOSE -> VERIFY.

The planner selects needs (metrics, registry formulas, computed expressions, named tools, PSX
APIs) from explicit menus; each need is fetched by a deterministic primitive; the composer writes
prose that the verifier gates, with a bounded open-web search as the terminal fallback. Returns
``(Response, QueryFrame, evidence)`` so the engine can wrap a TraceRecord.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from . import citations as citations_mod
from .models import QueryFrame, Response
from . import schemas, verify
from .observability import run_context
from .primitives import describe_workbook, execute_need, list_formulas

_log = logging.getLogger("app.engines.fie")

_MAX_NEEDS = 12  # bound the work a single plan can request


def _need_brief(n: dict) -> str:
    """One-line description of a plan need, for the FETCH/PLAN log lines."""
    k = n.get("kind")
    if k == "metric":
        return f"metric:{n.get('metric')}@{n.get('year')}"
    if k == "formula":
        return f"formula:{n.get('formula')}@{n.get('year')}"
    if k == "compute":
        return f"compute:{(n.get('expression') or '')[:40]}@{n.get('year')}"
    if k == "tool":
        args = ",".join(f"{kk}={vv}" for kk, vv in (n.get("args") or {}).items())
        return f"tool:{n.get('tool')}({args})"
    if k == "api":
        return f"api#{n.get('index')}" + (f"/{n.get('name')}" if n.get("name") else "")
    if k in ("news", "web"):
        return f"{k}:{(n.get('query') or '')[:60]!r}"
    return f"{k}"


def _res_brief(r: dict) -> str:
    """One-line summary of a fetched need's result, for the FETCH log lines."""
    if not isinstance(r, dict):
        return str(r)[:80]
    for key in ("value", "count", "net_margin_pct", "net_margin", "sector", "rank",
                "scenario", "from_value", "companies_total"):
        if r.get(key) is not None:
            return f"{key}={r.get(key)}"
    for key, label in (("companies", "companies"), ("ranked", "ranked"),
                       ("articles", "articles"), ("by_year", "years"), ("series", "years"),
                       ("rows", "rows"), ("insights", "insights")):
        v = r.get(key)
        if v is not None:
            try:
                return f"{label}={len(v)}"
            except TypeError:
                return f"{label}={v}"
    if r.get("lead"):
        return f"lead={r['lead'][:70]!r}"
    if r.get("note"):
        return f"note={r['note'][:90]!r}"
    return "ok"


def _dumps(items) -> list:
    """Serialize a list of pydantic models (evidence/calcs/citations) to plain dicts for a dump."""
    return [x.model_dump() if hasattr(x, "model_dump") else x for x in (items or [])]


def run(engine, query: str, *, audience: str = "analyst",
        history: list[dict] | None = None, pending_edits: list[dict] | None = None,
        now: str | None = None) -> tuple[Response, QueryFrame, list]:
    history = history or []
    trace_id = engine._trace_id() if hasattr(engine, "_trace_id") else uuid.uuid4().hex[:16]
    engine._last_trace_id = trace_id   # so answer_with_trace's TraceRecord matches the dump dir/log
    t0 = time.monotonic()
    with run_context(engine, trace_id, query, audience) as rl:
        llm = engine.llm   # the recording wrapper in DEBUG, the raw LLM otherwise
        # stash edit-history context where the edit_history primitive can read it
        engine._pending_edits = pending_edits or []
        engine._now = now
        menu = describe_workbook(engine)
        formulas = list_formulas(engine)

        # ---- PLAN: the LLM selects needs from the workbook/formula/tool/api menus ----
        rl.phase("plan")
        plan = _plan(llm, query, menu, formulas, history, rl)
        frame = _frame(query, plan, engine.store.company)
        # plan hints (company/sector/years/keywords) narrow PSX/web lookups for off-workbook asks.
        engine._hints = plan.get("hints") or {}
        needs = (plan.get("needs") or [])[:_MAX_NEEDS]
        _log.info("PLAN kind=%s needs=%d hints=%s interpretation=%r",
                  plan.get("answer_kind"), len(needs), engine._hints,
                  (plan.get("interpretation") or "")[:200], extra={"component": "Plan"})
        for i, need in enumerate(needs):
            _log.info("PLAN need[%d] %s", i, _need_brief(need), extra={"component": "Plan"})

        # ---- FETCH: run each need through its deterministic primitive; collect cited
        # evidence/calcs. EVERY query type (incl. availability + edit-history) flows through the
        # same pipeline. The edit-history payload (UI chips) rides through to the Response.
        evidence, calcs, fetched, gaps = [], [], [], []
        edit_payload = None
        fetch_dump = []
        for i, need in enumerate(needs):
            res, ev, c = execute_need(engine, need, frame)
            evidence += ev
            calcs += c
            fetched.append(res)
            gap = _is_gap(res)
            if res.get("kind") == "edit_history" and res.get("payload") is not None:
                edit_payload = res["payload"]
            if gap:
                gaps.append(res.get("note") or f"could not fetch {need}")
            _log.info("FETCH need[%d] %s -> %s | +%dev +%dcalc%s",
                      i, _need_brief(need), _res_brief(res), len(ev), len(c),
                      "  [GAP]" if gap else "", extra={"component": "Fetch"})
            fetch_dump.append({"need": need, "result": res, "gap": gap,
                               "evidence": _dumps(ev), "calcs": _dumps(c)})
        rl.dump("03_fetch", fetch_dump)
        rl.dump("04_evidence", _dumps(evidence))

        # ---- COMPOSE (+ sufficiency self-check) -> VERIFY (gates), with ONE bounded open-web
        # escalation. _compose owns citation binding because a web round adds evidence to re-bind.
        rl.phase("compose")
        direct, source, cites = _compose(llm, engine, query, fetched, gaps, frame,
                                         evidence, calcs, rl)
        findings = _findings(evidence, cites)
        resp = _response(direct, findings, calcs, cites, source)
        if edit_payload is not None:
            resp.edit_history = edit_payload  # UI renders change-chips from this, regardless of prose

        ms = round((time.monotonic() - t0) * 1000, 1)
        _log.info("DONE source=%s citations=%d findings=%d evidence=%d calcs=%d gaps=%d %.1fms",
                  source, len(cites), len(findings), len(evidence), len(calcs), len(gaps), ms,
                  extra={"component": "Respond"})
        rl.dump("00_summary", {
            "trace_id": trace_id, "query": query, "audience": audience,
            "company": engine.store.company, "answer_kind": plan.get("answer_kind"),
            "needs": [_need_brief(n) for n in needs], "gaps": gaps, "prose_source": source,
            "evidence": len(evidence), "calcs": len(calcs), "citations": len(cites),
            "findings": len(findings), "elapsed_ms": ms, "direct_answer": direct,
        })
        rl.dump("10_response", resp)
        return resp, frame, evidence


# --------------------------------------------------------------------------------- LLM steps
def _plan(llm, query, menu, formulas, history, rl) -> dict:
    from .external import list_apis
    from .tools import list_tools
    payload = {
        "question": query,
        "workbook": menu,
        "formulas": formulas,
        "tools": list_tools(),
        "apis": list_apis(),
        "recent": [h.get("text", "") for h in history[-6:] if h.get("role") == "user"],
    }
    rl.dump("01_plan_input", payload)   # exact menus + question sent to the planner LLM
    data = llm.complete_json(schemas.PLAN_SYS, json.dumps(payload, default=str), schemas.PLAN_SCHEMA)
    rl.dump("02_plan_output", data)     # raw planner output (interpretation, hints, needs)
    if isinstance(data, dict) and isinstance(data.get("needs"), list):
        return data
    # NullLLM / parse failure: no plan -> empty (compose will report it can't process)
    _log.warning("PLAN no usable plan (LLM returned %s) -> answering empty",
                 type(data).__name__, extra={"component": "Plan"})
    return {"needs": [], "answer_kind": "none"}


_QUALITATIVE_REFUSAL = ("I don't have the report's management commentary or insights loaded for "
                        "this workbook, so I can't answer that qualitative question from the "
                        "available data.")


def _web_enabled(engine) -> bool:
    try:
        from app.core.config import get_settings
        return bool(get_settings().fie_web_escalation)
    except Exception:  # noqa: BLE001 — config unavailable -> default on
        return True


def _compose(llm, engine, query, fetched, gaps, frame, evidence, calcs, rl) -> tuple[str, str, list]:
    """COMPOSE (LLM writes prose + self-rates sufficiency) -> VERIFY two gates. If the answer is
    INSUFFICIENT (low confidence, an unbacked number, or a sector/peer/qualitative claim with no
    external evidence), run ONE hosted web search using the LLM's hints, fold the cited results
    into evidence, and re-compose. The web round legitimises scope claims (real external evidence
    now exists) and backs any figure quoted verbatim. Returns (answer, source_label, citations).

    `evidence`/`calcs` are mutated in place so the caller's findings/citations see the web round.
    The gates remain the only correctness authority — the self-rated confidence merely TRIGGERS a
    search, it never lets prose ship unverified."""
    from .external import web_search, build_search_query
    allow_web = _web_enabled(engine)
    cites, _ = citations_mod.bind(evidence, calcs)
    escalated = False

    for attempt in range(2):  # at most: first pass + one post-web re-compose
        payload = {"question": query, "fetched": fetched, "missing": gaps}
        rl.dump(f"05_compose_input_{attempt}", payload)
        data = llm.complete_json(schemas.COMPOSE_SYS, json.dumps(payload, default=str),
                                 schemas.COMPOSE_SCHEMA)
        rl.dump(f"05_compose_output_{attempt}", data)
        if not (isinstance(data, dict) and (data.get("answer") or "").strip()):
            _log.warning("COMPOSE[%d] empty/invalid LLM answer -> deterministic fallback",
                         attempt, extra={"component": "Respond"})
            break
        direct = data["answer"].strip()
        conf = (data.get("confidence") or "").strip().lower()
        ok, reason = verify.grounding_ok(direct, evidence)
        numbers_pass = verify.numbers_ok(direct, frame, evidence, calcs, cites)
        insufficient = (not ok) or (not numbers_pass) or (conf == "low")
        rl.dump(f"06_verify_{attempt}", {
            "answer": direct, "self_confidence": conf, "grounding_ok": ok,
            "grounding_reason": reason, "numbers_pass": numbers_pass, "insufficient": insufficient,
            "search_query": data.get("search_query"), "hints": data.get("hints"),
        })
        _log.info("COMPOSE[%d] self_conf=%s grounding=%s%s numbers_pass=%s answer=%r", attempt,
                  conf or "?", ok, f"({reason})" if reason else "", numbers_pass, direct[:200],
                  extra={"component": "Respond"})

        # ESCALATE once to the open web when workbook/PSX evidence is insufficient.
        if insufficient and allow_web and not escalated:
            hints = (data.get("hints") or {}) or getattr(engine, "_hints", None) or {}
            q = (data.get("search_query") or "").strip() or build_search_query(query, hints)
            trigger = reason or ("low-confidence" if conf == "low" else "unbacked-number")
            rl.phase("web_search")
            res, web_ev, web_c = web_search(engine, q, hints=hints)
            rl.phase("compose")
            rl.dump("07_web_escalation", {"trigger": trigger, "query": q, "hints": hints,
                    "result": res, "added_evidence": len(web_ev)})
            if web_ev:
                evidence.extend(web_ev)
                calcs.extend(web_c)
                fetched.append(res)
                cites, _ = citations_mod.bind(evidence, calcs)
                escalated = True
                _log.info("COMPOSE[%d] insufficient (%s) -> web escalation: q=%r %d source(s)",
                          attempt, trigger, q, res.get("n_sources", 0),
                          extra={"component": "Respond"})
                continue  # re-compose now that web evidence is in scope
            _log.info("COMPOSE[%d] web escalation (%s) returned nothing (%s)",
                      attempt, trigger, res.get("note"), extra={"component": "Respond"})

        # No (further) escalation — resolve this attempt against the gates.
        if not ok and reason == "scope":
            _log.warning("COMPOSE[%d] sector/peer claim, no external evidence -> honest fallback",
                         attempt, extra={"component": "Respond"})
            rl.dump("08_citations", _dumps(cites))
            return _scope_fallback(fetched), "deterministic", cites
        if not ok and reason == "qualitative":
            _log.warning("COMPOSE[%d] qualitative claim, no insight/external evidence -> refusal",
                         attempt, extra={"component": "Respond"})
            rl.dump("08_citations", _dumps(cites))
            return _QUALITATIVE_REFUSAL, "deterministic", cites
        if numbers_pass:
            # prose_source is constrained to 'llm'|'deterministic'; the web round is logged above.
            rl.dump("08_citations", _dumps(cites))
            return direct, "llm", cites
        _log.warning("COMPOSE[%d] prose failed numeric guard -> deterministic fallback",
                     attempt, extra={"component": "Respond"})
        break

    rl.dump("08_citations", _dumps(cites))
    return _deterministic(fetched, gaps), "deterministic", cites


# ----------------------------------------------------------------------------------- helpers
_CONTENT_KEYS = ("value", "series", "by_year", "net_margin", "sector", "checks_run",
                 "insights", "scenario", "from_value", "count", "articles", "rows", "lead")


def _is_gap(res: dict) -> bool:
    return not any(res.get(k) not in (None, [], {}) for k in _CONTENT_KEYS)


def _frame(query, plan, company) -> QueryFrame:
    needs = plan.get("needs") or []
    years = [n.get("year") for n in needs if isinstance(n.get("year"), int)]
    metrics = [n["metric"] for n in needs if n.get("metric")]
    return QueryFrame(raw_query=query, intent="agent", company=company,
                      year=(years[0] if years else None), metrics=metrics, source="llm")


def _findings(evidence, cites) -> list[str]:
    """Deterministic cited data points — built from evidence (never the LLM) so every key finding
    carries a real [Cn] handle (the Response invariant)."""
    out, seen = [], set()
    for e in evidence:
        if e.value is None or not e.citations:
            continue
        ref = e.citations[0].ref_id
        if not ref or not ref.startswith("C"):
            continue
        key = (e.claim, ref)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{e.claim} [{ref}]")
        if len(out) >= 8:
            break
    return out


def _deterministic(fetched, gaps) -> str:
    """Fallback answer when there's no usable LLM prose. Primitives that carry a ready-made
    `lead` (availability, edit-history) fall back to it; otherwise enumerate fetched values."""
    leads = [r["lead"] for r in fetched if r.get("lead")]
    if leads:
        return " ".join(leads)
    parts = []
    for r in fetched:
        name = r.get("formula") or r.get("metric") or r.get("label") or r.get("expression") or "value"
        if r.get("value") is not None:
            yr = f" {r['year']}" if r.get("year") is not None else ""
            parts.append(f"{name}{yr} = {r['value']}")
        elif r.get("by_year"):
            parts.append(f"{name}: " + ", ".join(f"{y}: {v}" for y, v in r["by_year"].items()))
        elif r.get("series"):
            parts.append(f"{name}: " + ", ".join(f"{y}: {v}" for y, v in r["series"].items()))
    if not parts:
        msg = "I couldn't find the data needed to answer that in the workbook."
        return msg + (f" Missing: {'; '.join(gaps)}." if gaps else "")
    return "From the workbook — " + "; ".join(parts) + "."


def _scope_fallback(fetched) -> str:
    """Honest answer when the question asked for sector/peer/industry data but we only have the
    company's own workbook figures — state the limit, then offer the company figure we do have."""
    have = []
    for r in fetched:
        name = r.get("formula") or r.get("metric") or r.get("expression") or "figure"
        if r.get("value") is not None:
            have.append(f"the company's {name} was {r['value']}"
                        + (f" in {r['year']}" if r.get("year") is not None else ""))
        elif r.get("series") or r.get("by_year"):
            vals = r.get("series") or r.get("by_year")
            have.append(f"the company's {name} by year: "
                        + ", ".join(f"{y}: {v}" for y, v in vals.items()))
    tail = (" " + "; ".join(have) + ".") if have else ""
    return ("I can't answer that at the sector/peer level — this workbook holds only the "
            "company's own figures, not sector or peer data." + tail)


def _response(direct, findings, calcs, cites, source) -> Response:
    return Response(direct_answer=direct, key_findings=findings, calculations=calcs,
                    citations=cites, prose_source=source)
