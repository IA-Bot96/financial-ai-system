"""The engine controller: PLAN -> FETCH -> COMPOSE -> VERIFY.

The planner selects needs (metrics, registry formulas, computed expressions, named tools, PSX
APIs) from explicit menus; each need is fetched by a deterministic primitive; the composer writes
prose that the verifier gates, with a bounded open-web search as the terminal fallback. Returns
``(Response, QueryFrame, evidence)`` so the engine can wrap a TraceRecord.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from . import citations as citations_mod
from .models import CalcResult, Citation, ConfidenceReport, EvidenceItem, QueryFrame, Response
from . import schemas, verify
from .observability import run_context
from .pricing import usage_cost, usage_snapshot
from .primitives import describe_workbook, execute_need, list_formulas

_log = logging.getLogger("app.engines.fie")

_MAX_NEEDS = 24  # bound the work a single plan can request (room for compound asks + per-member
#                  fan-out across a sector, e.g. one getCompanyOverview need per listed company)
_MAX_COMPOSE_ROUNDS = 3  # agentic loop: initial compose + up to 2 re-fetch+re-compose rounds.
#                          Bounds cost/termination; dedup of needs stops redundant re-fetches.
# WEB PHASE (after rule-based is exhausted) runs at most twice: (1) a rule-based hosted search with
# the LLM's query, then (2) — only if still incomplete — the composer searches the web itself.


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


_RECENT_TURNS = 12          # how many prior messages to send (~6 exchanges)
_RECENT_ANSWER_CHARS = 1000  # per-answer cap — generous enough to keep lists/figures intact


def _recent_context(history) -> list[dict]:
    """The REAL prior conversation for the planner (redesign §10) — a uniform list of
    {timestamp?, role, content}, verbatim (assistant content capped to keep the prompt bounded).
    The planner resolves follow-ups, pronouns, ellipsis and references by reading this conversation
    NATURALLY, like a chat assistant — so we do NOT pre-digest it into structured fields. (That lossy
    summary lost context and injected false signals, e.g. a founding year or a stale company leaking
    into the next turn.) This block is placed FIRST in the payload (before the catalogs) so it is
    never buried under the menus."""
    out: list[dict] = []
    for h in (history or [])[-_RECENT_TURNS:]:
        text = (h.get("text") or "").strip()
        role = h.get("role")
        if not text or role not in ("user", "assistant"):
            continue
        content = text if role == "user" else text[:_RECENT_ANSWER_CHARS]
        msg = {"role": role, "content": content}
        ts = h.get("timestamp") or h.get("time") or h.get("ts")
        if ts:
            msg = {"timestamp": ts, **msg}
        out.append(msg)
    return out


_PLACEHOLDER = {"unknown", "n/a", "none", "null", ""}


def _merge_hints(*sources) -> dict:
    """Merge hint dicts in priority order (earlier wins), skipping placeholder/empty values. Used so
    the web-escalation query draws company/sector from the reliable PLAN hints (derived from the
    workbook + question) rather than the COMPOSE LLM's often-'unknown' self-reported hints."""
    out: dict = {}
    for src in sources:
        for k, v in (src or {}).items():
            if out.get(k) not in (None, "", []):
                continue                                  # an earlier (higher-priority) source set it
            if isinstance(v, str) and v.strip().lower() in _PLACEHOLDER:
                continue
            if v in (None, "", []):
                continue
            out[k] = v
    return out


def run(engine, query: str, *, audience: str = "analyst",
        history: list[dict] | None = None, pending_edits: list[dict] | None = None,
        now: str | None = None) -> tuple[Response, QueryFrame, list]:
    history = history or []
    trace_id = engine._trace_id() if hasattr(engine, "_trace_id") else uuid.uuid4().hex[:16]
    engine._last_trace_id = trace_id   # so answer_with_trace's TraceRecord matches the dump dir/log
    t0 = time.monotonic()
    with run_context(engine, trace_id, query, audience) as rl:
        llm = engine.llm   # the recording wrapper in DEBUG, the raw LLM otherwise
        usage_before = usage_snapshot(llm)   # diff'd at the end to bill this query's tokens
        # stash edit-history context where the edit_history primitive can read it
        engine._pending_edits = pending_edits or []
        engine._now = now
        from .tools import list_tools
        menu = describe_workbook(engine)
        formulas = list_formulas(engine)
        tools_menu = list_tools()
        valid = _menu_valid(menu, formulas, tools_menu)   # offered-id sets, for D4 + the compose loop
        # compact menu for COMPOSE so its follow-up `more_needs` copy REAL sheet/metric/formula/tool
        # names (it otherwise only sees fetched results and guesses 'balance_sheet'/'receivables').
        available = {"sheets": menu.get("sheets") or {},
                     "formulas": sorted(valid["formulas"]),
                     "tools": sorted(valid["tools"])}

        # ---- PLAN: the LLM selects needs from the workbook/formula/tool/api menus ----
        rl.phase("plan")
        plan = _plan(llm, query, menu, formulas, tools_menu, history, rl, valid)
        frame = _frame(query, plan, engine.store.company)
        # plan hints (company/sector/years/keywords) narrow PSX/web lookups for off-workbook asks.
        engine._hints = plan.get("hints") or {}
        needs = (plan.get("needs") or [])[:_MAX_NEEDS]
        # _plan() already logged the source-scoped plan summary + interpretation; here, the
        # resolved hints and the expanded per-need lines.
        _log.info("PLAN hints=%s", engine._hints, extra={"component": "Plan"})
        for i, need in enumerate(needs):
            _log.info("PLAN need[%d] %s", i, _need_brief(need), extra={"component": "Plan"})

        # ---- CLARIFY: genuinely ambiguous query -> ask ONE question instead of guessing. The
        # planner emits `clarification` with no needs; we return the question as the answer (no
        # FETCH/COMPOSE), with no confidence band (it's a question, not a graded answer).
        clarification = _text(plan.get("clarification")).strip()
        if clarification and not needs:
            _log.info("CLARIFY -> %r", clarification[:200], extra={"component": "Plan"})
            rl.dump("02b_clarification", {"question": clarification})
            resp = _response(clarification, [], [], [], "deterministic", confidence=None)
            rl.dump("10_response", resp)
            return resp, frame, []

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
        direct, source, cites, band, conf_reasons, completeness = _compose(
            llm, engine, query, fetched, gaps, frame, evidence, calcs, rl, valid, available)
        findings = _findings(evidence, cites)
        confidence = ConfidenceReport(band=band, score=_BAND_SCORE.get(band, 0.6),
                                      completeness=completeness, reasons=conf_reasons)
        resp = _response(direct, findings, calcs, cites, source, confidence)
        if edit_payload is not None:
            resp.edit_history = edit_payload  # UI renders change-chips from this, regardless of prose
        resp.usage = usage_cost(llm, usage_before)

        ms = round((time.monotonic() - t0) * 1000, 1)
        u = resp.usage
        _log.info("DONE source=%s confidence=%s citations=%d findings=%d evidence=%d calcs=%d "
                  "gaps=%d %.1fms | cost=%s",
                  source, band, len(cites), len(findings), len(evidence), len(calcs), len(gaps), ms,
                  (f"${u.total_usd:.6f} ({u.prompt_tokens}+{u.completion_tokens} tok, "
                   f"{u.api_calls} call(s), {u.cached_calls} cached, model={u.model})"
                   if u else "n/a (no LLM)"),
                  extra={"component": "Respond"})
        rl.dump("00_summary", {
            "trace_id": trace_id, "query": query, "audience": audience,
            "company": engine.store.company, "answer_kind": plan.get("answer_kind"),
            "needs": [_need_brief(n) for n in needs], "gaps": gaps, "prose_source": source,
            "confidence": band, "confidence_reasons": conf_reasons,
            "evidence": len(evidence), "calcs": len(calcs), "citations": len(cites),
            "findings": len(findings), "elapsed_ms": ms, "direct_answer": direct,
            "usage": u.model_dump() if u else None,
        })
        rl.dump("10_response", resp)
        return resp, frame, evidence


# --------------------------------------------------------------------------------- LLM steps
def _menu_valid(menu, formulas, tools_menu) -> dict:
    """The sets of ids actually offered to the planner — used to drop hallucinated selections (D4).
    Shared by the planner and the agentic compose loop (so follow-up needs are validated too)."""
    return {
        "sheets": set((menu.get("sheets") or {}).keys()),
        "metrics": {m for ms in (menu.get("sheets") or {}).values() for m in ms},
        "formulas": {f["id"] for f in formulas if f.get("id")},
        "tools": {t.get("name") for t in tools_menu},
    }


def _text(v) -> str:
    """Coerce an LLM field that SHOULD be a string but sometimes isn't (the model returns a dict or
    list despite the schema) into a safe string — so a mis-typed field can never crash the request."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("question", "text", "message", "answer", "value"):
            if isinstance(v.get(k), str):
                return v[k]
        return ""
    return "" if v is None else str(v)


def _need_sig(n: dict):
    """A signature for de-duplicating needs across agentic rounds (don't re-fetch the same thing)."""
    if n.get("kind") == "tool":
        return ("tool", n.get("tool"), tuple(sorted((n.get("args") or {}).items())))
    return (n.get("kind"), n.get("metric") or n.get("formula") or n.get("expression")
            or n.get("query"), n.get("year"))


def _plan(llm, query, menu, formulas, tools_menu, history, rl, valid) -> dict:
    # `recent_messages` is placed FIRST (right after the question), before the sheets/formulas/tools
    # catalogs — burying it after ~30 KB of menus is what made the planner miss the conversation.
    payload = {
        "question": query,
        "recent_messages": _recent_context(history),
        "workbook": menu,
        "formulas": formulas,
        "tools": tools_menu,
    }
    rl.dump("01_plan_input", payload)   # exact menus + question sent to the planner LLM
    data = llm.complete_json(schemas.PLAN_SYS, json.dumps(payload, default=str, separators=(",", ":")),
                             schemas.PLAN_SCHEMA)
    rl.dump("02_plan_output", data)     # raw planner output (source-scoped plan)
    if not isinstance(data, dict):
        # NullLLM / parse failure: no plan -> empty (compose will report it can't process)
        _log.warning("PLAN no usable plan (LLM returned %s) -> answering empty",
                     type(data).__name__, extra={"component": "Plan"})
        return {"needs": [], "answer_kind": "none"}
    # ADAPTER: expand the source-scoped plan into the internal needs[] the fetch layer consumes,
    # so execute_need / _frame stay unchanged. D4 guards run here, validating the planner's
    # selections against the menus actually offered (drop hallucinated ids / placeholder args).
    data["needs"] = _plan_to_needs(data, valid)
    # `hints.company`/`sector` are scalar by schema, but the LLM sometimes returns the whole subject
    # set as a list there — coerce to a single string (or None) so downstream (frame, web search,
    # _merge_hints) never sees a list. The per-need tool args still carry the full company set.
    h = data.get("hints")
    if isinstance(h, dict):
        for k in ("company", "sector"):
            val = h.get(k)
            if isinstance(val, (list, tuple)):
                h[k] = next((x for x in val if isinstance(x, str) and x.strip()), None) if len(val) == 1 \
                    else None
    _log.info("PLAN %s | interpretation=%r%s", _plan_brief(data),
              _text(data.get("interpretation"))[:200],
              f" | clarification={data['clarification']!r}" if data.get("clarification") else "",
              extra={"component": "Plan"})
    return data


def _plan_brief(plan: dict) -> str:
    """One-line summary of the SOURCE-SCOPED plan (before adapter expansion) for the trace log —
    shows which sources the planner populated, so 'did it fan out / stay on workbook' is visible."""
    fin = plan.get("financial") or []
    n_fin_metrics = sum(len(b.get("metrics") or []) for b in fin if isinstance(b, dict))
    parts = [
        f"financial={len(fin)}blk/{n_fin_metrics}m",
        f"formulas={len(plan.get('formulas') or [])}",
        f"compute={len(plan.get('compute') or [])}",
        f"tools={len(plan.get('tools') or [])}",
        f"forecast={len(plan.get('forecast') or [])}",
        f"insights={'Y' if isinstance(plan.get('insights'), dict) else 'n'}",
        f"validation={'Y' if isinstance(plan.get('validation'), dict) else 'n'}",
        f"edit_history={'Y' if plan.get('edit_history') is not None else 'n'}",
        f"news={len(plan.get('news') or [])}",
        f"web={len(plan.get('web') or [])}",
        f"years={[y for y in (plan.get('years') or []) if isinstance(y, int)]}",
        f"-> needs={len(plan.get('needs') or [])}",
    ]
    return " ".join(parts)


# entity refs the planner sometimes leaves UNRESOLVED in a tool arg instead of fanning out — a
# literal company is a name/ticker, never a phrase like these (D4(a)).
_ARG_PLACEHOLDER_RE = re.compile(
    r"\b(each|every|all of|the (prior|above|previous|other|same|comparison)|comparison set|"
    r"same company|as before|those companies|these companies|prior comparison|"
    r"from the (comparison|prior|above))\b", re.I)


def _is_placeholder_arg(v) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip().lower()
    return s in _PLACEHOLDER or bool(_ARG_PLACEHOLDER_RE.search(s))


def _plan_to_needs(plan: dict, valid: dict | None = None) -> list[dict]:
    """Translate the source-scoped plan (redesign §4/§12) into the internal needs[] list. `financial`
    and `formulas` cross-product with the shared `years`; `tools` are dropped if any arg is an
    unresolved placeholder phrase (D4(a)). When `valid` is given (the menus actually offered), the
    planner's selections are checked against it and hallucinated ids are dropped + logged (D4)."""
    needs: list[dict] = []
    years = [y for y in (plan.get("years") or []) if isinstance(y, int)]
    v_sheets = (valid or {}).get("sheets")
    v_metrics = (valid or {}).get("metrics")
    v_formulas = (valid or {}).get("formulas")
    v_tools = (valid or {}).get("tools")

    def yspan(yrs):
        return [y for y in yrs if isinstance(y, int)] or [None]

    def _drop(what, value):
        _log.warning("PLAN dropped %s not in menu: %r", what, value, extra={"component": "Plan"})

    for block in plan.get("financial") or []:
        if not isinstance(block, dict):
            continue
        sheet = block.get("sheet")
        if v_sheets is not None and sheet not in v_sheets:
            _drop("financial.sheet", sheet)
            continue
        for m in block.get("metrics") or []:
            if v_metrics is not None and m not in v_metrics:
                _drop("financial.metric", m)
                continue
            for y in yspan(years):
                needs.append({"kind": "metric", "metric": m, "year": y})
    for fid in plan.get("formulas") or []:
        if v_formulas is not None and fid not in v_formulas:
            _drop("formula", fid)
            continue
        for y in yspan(years):
            needs.append({"kind": "formula", "formula": fid, "year": y})
    for c in plan.get("compute") or []:
        if not isinstance(c, dict) or not c.get("expression"):
            continue
        for y in yspan(c.get("years") or []):
            needs.append({"kind": "compute", "expression": c["expression"],
                          "label": c.get("label"), "year": y})
    if isinstance(plan.get("insights"), dict):
        needs.append({"kind": "insights"})
    if isinstance(plan.get("validation"), dict):
        needs.append({"kind": "validation"})
    if plan.get("edit_history") is not None:
        needs.append({"kind": "edit_history"})
    for f in plan.get("forecast") or []:
        if isinstance(f, dict) and f.get("metric") and isinstance(f.get("year"), int):
            needs.append({"kind": "forecast", "metric": f["metric"], "year": f["year"],
                          "growth": f.get("growth")})
    for t in plan.get("tools") or []:
        if not isinstance(t, dict) or not t.get("tool"):
            continue
        if v_tools is not None and t["tool"] not in v_tools:
            _drop("tool", t["tool"])
            continue
        args = t.get("args") or {}
        if any(_is_placeholder_arg(v) for v in args.values()):
            _log.warning("PLAN dropped tool %s with placeholder args %s",
                         t.get("tool"), args, extra={"component": "Plan"})
            continue
        needs.append({"kind": "tool", "tool": t["tool"], "args": args})
    for a in plan.get("aggregate") or []:
        if isinstance(a, dict) and a.get("op") and isinstance(a.get("values"), list):
            needs.append({"kind": "aggregate", "op": a["op"], "values": a["values"],
                          "label": a.get("label"), "unit": a.get("unit")})
    for n in plan.get("news") or []:
        if isinstance(n, dict) and n.get("query"):
            needs.append({"kind": "news", "query": n["query"]})
    for w in plan.get("web") or []:
        if isinstance(w, dict) and w.get("query"):
            needs.append({"kind": "web", "query": w["query"]})
    return needs[:_MAX_NEEDS]


def _completeness_threshold(engine) -> float:
    try:
        from app.core.config import get_settings
        return float(get_settings().fie_completeness_threshold)
    except Exception:  # noqa: BLE001 — config unavailable -> sensible default
        return 0.8


def _flatten_numbers(obj, out: list) -> None:
    """Collect every numeric value reachable in a fetched-result structure (for the aggregate
    safeguard — does the LLM-listed value actually appear in what we fetched?). Not regex."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_numbers(v, out)


def _aggregate(need: dict, fetched: list, evidence: list):
    """Rule-based mean/sum/min/max over values the COMPOSER listed (the LLM never computes the
    figure — it only lists the inputs + op; the engine does the arithmetic and emits a cited calc).
    Safeguard: prefer values that actually appear in the fetched data, so a hallucinated number
    can't slip into the aggregate."""
    op = (need.get("op") or "").strip().lower()
    vals = [v for v in (need.get("values") or [])
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if op not in ("mean", "sum", "min", "max") or not vals:
        return None
    known: list = []
    for f in fetched:
        _flatten_numbers(f, known)
    for e in evidence:
        ev = getattr(e, "value", None)
        if isinstance(ev, (int, float)) and not isinstance(ev, bool):
            known.append(float(ev))
    matched = [v for v in vals if any(abs(v - k) <= 1e-6 * max(1.0, abs(k)) for k in known)]
    used = matched or vals
    if not matched:
        _log.warning("AGGREGATE values not found in fetched data: %s", vals,
                     extra={"component": "Respond"})
    res = (sum(used) / len(used) if op == "mean" else sum(used) if op == "sum"
           else min(used) if op == "min" else max(used))
    label = need.get("label") or f"{op} of {len(used)} values"
    result = {"kind": "aggregate", "op": op, "label": label, "unit": need.get("unit"),
              "value": res, "components": used}
    return result, CalcResult(formula_id="aggregate", value=float(res), confidence="High")


def _web_sources_to_evidence(sources) -> list:
    """Turn hosted-search citation sources ({url,title,snippet}) into cited external EvidenceItems —
    the same shape external.web_search produces — so the LLM's OWN web citations become traceable."""
    out = []
    for s in sources or []:
        url = s.get("url")
        snip = (s.get("snippet") or "")[:1000]
        out.append(EvidenceItem(
            claim=(s.get("title") or url or "source")[:200], value=None, unit=None, kind="external",
            citations=[Citation(ref_id="C?", kind="external", display=(s.get("title") or url),
                                locator={"source": "web", "url": url, "link": url, "snippet": snip})],
            reliability=0.6))
    return out


_QUALITATIVE_REFUSAL = ("I don't have the report's management commentary or insights loaded for "
                        "this workbook, so I can't answer that qualitative question from the "
                        "available data.")


_BAND_SCORE = {"High": 0.9, "Medium": 0.6, "Low": 0.3}


def _confidence_band(grounding_ok: bool, numbers_pass: bool) -> tuple[str, list[str]]:
    """The two verify gates GRADE the answer (they no longer reject it): both pass -> High, one
    fails -> Medium, both fail -> Low. Returns the band + human reasons for the failing gate(s)."""
    passed = int(bool(grounding_ok)) + int(bool(numbers_pass))
    band = "High" if passed == 2 else "Medium" if passed == 1 else "Low"
    reasons: list[str] = []
    if not grounding_ok:
        reasons.append("scope/qualitative grounding check did not pass")
    if not numbers_pass:
        reasons.append("a stated figure could not be traced to a cited source")
    return band, reasons


def _web_enabled(engine) -> bool:
    try:
        from app.core.config import get_settings
        return bool(get_settings().fie_web_escalation)
    except Exception:  # noqa: BLE001 — config unavailable -> default on
        return True


def _compose(llm, engine, query, fetched, gaps, frame, evidence, calcs, rl, valid, available=None
             ) -> tuple[str, str, list, str, list, float]:
    """COMPOSE inside a BOUNDED AGENTIC LOOP. Each round the composer writes prose and self-rates
    `completeness` (0..1). While completeness is below the configured threshold (and rounds remain),
    the engine re-fetches the composer's requested RULE-BASED needs (tools / workbook / aggregate —
    NO web mid-loop) through the same adapter + primitives, de-duplicating so it can't re-fetch the
    same thing, and re-composes. Once the rule-based rounds are exhausted and it is STILL incomplete,
    ONE terminal open-web search (LLM-supplied query) runs as the last resort, then a final compose.

    Returns (answer, source_label, citations, confidence_band, reasons, completeness). The two verify
    gates GRADE the answer; a figure that cannot be traced to a citation forces the band to Low and
    the unsourced prose is replaced by the verifiable-data answer."""
    from .external import web_search, build_search_query
    allow_web = _web_enabled(engine)
    threshold = _completeness_threshold(engine)
    cites, _ = citations_mod.bind(evidence, calcs)
    seen: set = set()                       # need signatures already fetched (dedup across rounds)
    direct = conf = ""
    ok, reason, numbers_pass, completeness, got, last_query = True, None, True, 1.0, False, ""

    def _run(tag):
        # `available` tells the composer the EXACT sheet/metric/formula/tool names it may request in
        # more_needs (so it copies them verbatim instead of guessing).
        payload = {"question": query, "fetched": fetched, "missing": gaps, "available": available or {}}
        rl.dump(f"05_compose_input_{tag}", payload)
        d = llm.complete_json(schemas.COMPOSE_SYS,
                              json.dumps(payload, default=str, separators=(",", ":")),
                              schemas.COMPOSE_SCHEMA)
        rl.dump(f"05_compose_output_{tag}", d)
        return d if isinstance(d, dict) else {}

    def _grade(data, tag):
        nonlocal direct, conf, ok, reason, numbers_pass, completeness, got, last_query, cites
        d = _text(data.get("answer")).strip()
        if not d:
            return False
        direct, got = d, True
        conf = _text(data.get("confidence")).strip().lower()
        c = data.get("completeness")
        completeness = float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else 1.0
        last_query = _text(data.get("search_query")).strip()
        ok, reason = verify.grounding_ok(direct, evidence)
        numbers_pass = verify.numbers_ok(direct, frame, evidence, calcs, cites)
        rl.dump(f"06_verify_{tag}", {
            "answer": direct, "self_confidence": conf, "completeness": completeness,
            "grounding_ok": ok, "grounding_reason": reason, "numbers_pass": numbers_pass,
            "more_needs": data.get("more_needs"), "search_query": last_query})
        _log.info("COMPOSE[%s] conf=%s completeness=%.2f grounding=%s%s numbers=%s answer=%r",
                  tag, conf or "?", completeness, ok, f"({reason})" if reason else "", numbers_pass,
                  direct[:200], extra={"component": "Respond"})
        return True

    # --- RULE-BASED agentic rounds (no web mid-loop) ---
    for rnd in range(_MAX_COMPOSE_ROUNDS):
        data = _run(rnd)
        if not _grade(data, rnd):
            _log.warning("COMPOSE[%d] empty/invalid LLM answer -> deterministic fallback",
                         rnd, extra={"component": "Respond"})
            break
        if completeness >= threshold or rnd >= _MAX_COMPOSE_ROUNDS - 1:
            break
        more = data.get("more_needs") if isinstance(data.get("more_needs"), dict) else {}
        # rule-based loop: news IS allowed; WEB is held for the post-loop web phase (the open web is
        # tried ONLY after rule-based sources are exhausted).
        new_needs = [n for n in _plan_to_needs(more, valid) if n.get("kind") != "web"]
        fresh = [n for n in new_needs if _need_sig(n) not in seen]
        for n in fresh:
            seen.add(_need_sig(n))
        if not fresh:
            break                              # no new rule-based data to try -> terminal web
        rl.phase("refetch")
        refetch_dump = []
        for need in fresh:
            if need.get("kind") == "aggregate":      # engine computes mean/sum over fetched values
                agg = _aggregate(need, fetched, evidence)
                if agg:
                    res, c = agg
                    fetched.append(res)
                    calcs.append(c)
                    refetch_dump.append({"need": need, "result": res, "gap": False})
                continue
            res, ev, c = execute_need(engine, need, frame)
            evidence += ev
            calcs += c
            fetched.append(res)
            if _is_gap(res):
                gaps.append(res.get("note") or f"could not fetch {need}")
            refetch_dump.append({"need": need, "result": res, "gap": _is_gap(res),
                                 "evidence": _dumps(ev)})
        cites, _ = citations_mod.bind(evidence, calcs)
        rl.dump(f"07_refetch_{rnd}", refetch_dump)
        rl.phase("compose")
        _log.info("COMPOSE[%d] incomplete (%.2f<%.2f) -> re-fetched %d rule-based need(s): %s",
                  rnd, completeness, threshold, len(fresh), [_need_brief(n) for n in fresh],
                  extra={"component": "Respond"})

    # --- WEB PHASE (only after rule-based rounds are exhausted) ---
    # (1) RULE-BASED web search: the engine runs the hosted search with the LLM's query, folds the
    #     cited sources into evidence, and re-composes.
    if got and completeness < threshold and allow_web:
        q = last_query or build_search_query(query, getattr(engine, "_hints", None) or {})
        if q:
            rl.phase("web_search")
            res, web_ev, web_c = web_search(engine, q, hints=getattr(engine, "_hints", None) or {})
            rl.phase("compose")
            rl.dump("07_web_rulebased", {"query": q, "result": res, "added_evidence": len(web_ev)})
            if web_ev:
                evidence.extend(web_ev)
                calcs.extend(web_c)
                fetched.append(res)
                cites, _ = citations_mod.bind(evidence, calcs)
                _log.info("COMPOSE web (rule-based) q=%r -> %d source(s)", q,
                          res.get("n_sources", 0), extra={"component": "Respond"})
                _grade(_run("web_rb"), "web_rb")

    # (2) LLM-DRIVEN web search: if the rule-based web result STILL isn't enough, let the composer
    #     search the open web ITSELF (hosted web_search tool) while writing the answer; its own
    #     citation annotations become cited evidence.
    if got and completeness < threshold and allow_web and hasattr(llm, "complete_json_web"):
        payload = {"question": query, "fetched": fetched, "missing": gaps, "available": available or {}}
        rl.dump("05_compose_input_web_llm", payload)
        data2, sources = llm.complete_json_web(
            schemas.COMPOSE_SYS, json.dumps(payload, default=str, separators=(",", ":")),
            schemas.COMPOSE_SCHEMA)
        rl.dump("05_compose_output_web_llm", {"data": data2, "sources": len(sources or [])})
        if sources:
            evidence.extend(_web_sources_to_evidence(sources))
            cites, _ = citations_mod.bind(evidence, calcs)
        _log.info("COMPOSE web (LLM-driven) -> %d source(s) json=%s",
                  len(sources or []), bool(data2), extra={"component": "Respond"})
        if isinstance(data2, dict):
            rl.phase("compose")
            _grade(data2, "web_llm")

    # --- FINALIZE ---
    if not got:
        # No usable LLM prose (empty reply / NullLLM): deterministic answer is the workbook's own
        # values — ground truth (High) — unless there was nothing answerable to show (Low).
        rl.dump("08_citations", _dumps(cites))
        det = _deterministic(fetched, gaps)
        if det.startswith("I couldn't find"):
            return det, "deterministic", cites, "Low", ["no answerable data found in the workbook"], 0.0
        return det, "deterministic", cites, "High", [], completeness
    band, reasons = _confidence_band(ok, numbers_pass)
    if not numbers_pass:
        band = "Low"            # a figure could not be traced to a citation -> Low (per policy)
    rl.dump("08_citations", _dumps(cites))
    _log.info("COMPOSE final -> confidence=%s completeness=%.2f (grounding=%s numbers=%s)",
              band, completeness, ok, numbers_pass, extra={"component": "Respond"})
    if not ok and reason == "scope":
        return (_scope_fallback(fetched), "deterministic", cites, band,
                ["only the company's own figures are available (not sector/peer)"] + reasons, completeness)
    if not ok and reason == "qualitative":
        return (_QUALITATIVE_REFUSAL, "deterministic", cites, band,
                ["no report commentary/insights loaded for this workbook"] + reasons, completeness)
    if numbers_pass:
        return direct, "llm", cites, band, reasons, completeness
    return (_deterministic(fetched, gaps), "deterministic", cites, band,
            ["a stated figure could not be traced to a source; showing only verifiable values"]
            + reasons, completeness)


# ----------------------------------------------------------------------------------- helpers
_CONTENT_KEYS = ("value", "series", "by_year", "net_margin", "sector", "checks_run",
                 "insights", "scenario", "from_value", "count", "articles", "rows", "lead")


def _is_gap(res: dict) -> bool:
    return not any(res.get(k) not in (None, [], {}) for k in _CONTENT_KEYS)


def _subject_companies(needs, hints, workbook_company) -> list[str]:
    """The SUBJECT SET — every company the turn was about, in order: the planner's hint company
    first, then any company named in a tool need's args (so 'Millat vs Lucky' keeps BOTH). Falls
    back to the workbook company when nothing else is named (a list of one)."""
    out: list[str] = []

    def add(c):
        if isinstance(c, (list, tuple)):       # planner may put the whole subject set in one field
            for x in c:
                add(x)
            return
        if not isinstance(c, str):
            return
        c = c.strip()
        if c and c.lower() not in _PLACEHOLDER and c not in out:
            out.append(c)

    add((hints or {}).get("company"))
    for n in needs:
        add((n.get("args") or {}).get("company"))
    return out or [workbook_company]


def _frame(query, plan, company) -> QueryFrame:
    needs = plan.get("needs") or []
    hints = plan.get("hints") or {}
    yrs = sorted({n["year"] for n in needs if isinstance(n.get("year"), int)})
    metrics = [n["metric"] for n in needs if n.get("metric")]
    tools = [n["tool"] for n in needs if n.get("tool")]
    formulas = [n["formula"] for n in needs if n.get("formula")]
    companies = _subject_companies(needs, hints, company)  # the SUBJECT SET (keeps comparisons)
    sector = (hints.get("sector") or "").strip() or None
    if sector and sector.lower() in _PLACEHOLDER:
        sector = None
    return QueryFrame(
        raw_query=query, intent="agent",
        company=companies[0], companies=companies,       # primary + the full set
        sector=sector,
        year=(yrs[-1] if yrs else None),                 # operative point year (latest requested)
        years=(yrs if len(yrs) > 1 else []),             # explicit range only when multi-year
        metrics=metrics, formula=(formulas[0] if formulas else None), formulas=formulas,
        tool=(tools[0] if tools else None), tools=tools, source="llm")


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


def _response(direct, findings, calcs, cites, source, confidence=None) -> Response:
    return Response(direct_answer=direct, key_findings=findings, calculations=calcs,
                    citations=cites, prose_source=source, confidence=confidence)
