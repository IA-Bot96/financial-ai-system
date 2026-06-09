"""FIE orchestrator (Phase 4).

Routes by intent through the deterministic layers, optional LLM, and now external
sources (PSX / News / Forecast) plus peer (multi-workbook) comparison. External
fetches degrade gracefully: on failure the engine proceeds on internal data and
caps confidence (architecture §3.2, §4, §9.2).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta
from typing import Callable

from app.core.debug import make_fie_dumper
from app.core.logging import per_query_log
from . import citations as citations_mod
from . import insights as insights_mod
from .debug_dump import restore_llms, wrap_llms
from . import (admission, agent, entity_registry, forecast_rules, metric_resolve,
               news_retrieval, planner, qualitative, response, retrieval, scale,
               synthesis, understanding)
from .apis import ExternalSources
from .calc import CalcEngine
from .conflicts import ConflictResolver
from .confidence import ConfidenceScorer
from .llm import NullLLM
from .models import CalcResult, EvidenceItem, Response
from .store import FinancialFactStore
from .trace import TraceRecord
from .understanding import COMPANY_TICKER

_log = logging.getLogger("app.engines.fie")


def _layer(component: str, msg: str, *args) -> None:
    _log.info(msg, *args, extra={"component": component})


# A metric_lookup phrased as "... value AND percentage", "what percent of revenue", etc. needs
# the denominator fetched so the share can be computed (see _attach_percentage).
_PCT_REQUEST_RE = re.compile(r"\bpercent\w*\b|\bproportion\b|\bfraction\b|%", re.I)

# --- edit_history query parsing (temporal / sheet filters) ---------------------------------
_HIST_WIN_RE = re.compile(r"\b(?:last|past|within|in|over|in the last|in the past|over the last)\s+"
                          r"(\d{1,4})\s*(min|minute|hour|hr|day|week)s?\b", re.I)
_HIST_LIMIT_RE = re.compile(r"\b(?:last|recent|latest)\s+(\d{1,3})\b", re.I)
_HIST_ONE_RE = re.compile(r"\b(?:last|latest|recent|most recent)\s+"
                          r"(?:change|edit|modification|update)\b", re.I)
_HIST_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_HIST_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?\s+(\d{4})\b", re.I)
_HIST_DATE_MDY = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
_HIST_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_HIST_DATE_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _hist_parse_dt(s):
    """Tolerant timestamp parse for History-sheet / pending-edit rows (-> datetime|None)."""
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    s2 = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            continue
    return None


def _hist_parse_query_date(q):
    """Parse an explicit calendar date out of a query ('31 Aug 2025', 'Aug 31 2025',
    '2025-08-31', '31/08/2025') -> datetime|None (day precision)."""
    m = _HIST_DATE_ISO.search(q)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            pass
    m = _HIST_DATE_DMY.search(q)
    if m and m.group(2).lower()[:3] in _HIST_MONTHS:
        try:
            return datetime(int(m.group(3)), _HIST_MONTHS[m.group(2).lower()[:3]], int(m.group(1)))
        except ValueError:
            pass
    m = _HIST_DATE_MDY.search(q)
    if m and m.group(1).lower()[:3] in _HIST_MONTHS:
        try:
            return datetime(int(m.group(3)), _HIST_MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        except ValueError:
            pass
    m = _HIST_DATE_SLASH.search(q)
    if m:
        try:
            return datetime(int(m[3]), int(m[2]), int(m[1]))   # d/m/y
        except ValueError:
            pass
    return None


def ev_over_ebitda(price, shares, ebitda, debt, cash) -> dict | None:
    """EV/EBITDA = (market_cap + net_debt) / EBITDA. Returns None unless price,
    shares and EBITDA are all present (so it degrades silently). ``debt`` is a
    total-liabilities proxy; ``cash`` may be None (treated as 0)."""
    if not price or not shares or not ebitda:
        return None
    market_cap = price * shares
    net_debt = (debt or 0.0) - (cash or 0.0)
    ev = market_cap + net_debt
    return {"ev_ebitda": round(ev / ebitda, 4), "ev": ev,
            "market_cap": market_cap, "net_debt": net_debt}


class FinancialIntelligenceEngine:
    def __init__(self, store: FinancialFactStore, *, llm=None,
                 external: ExternalSources | None = None,
                 insight_mode: str = "year_then_confidence", alpha: float = 0.7,
                 trace_id_factory: Callable[[], str] | None = None) -> None:
        self.store = store
        self.llm = llm or NullLLM()
        self.external = external or ExternalSources()
        self._trace_id = trace_id_factory or (lambda: uuid.uuid4().hex[:16])
        self.calc = CalcEngine(store)
        self.conflicts = ConflictResolver(store, llm=self.llm)
        self.confidence = ConfidenceScorer()
        self.insights = insights_mod.InsightSelector(mode=insight_mode, alpha=alpha,
                                                     llm=self.llm)
        self.synthesizer = synthesis.Synthesizer(llm=self.llm)
        self._registry = None              # lazy EntityRegistry over PSX symbols
        self._last_entity_verdict = None   # last company->ticker resolution verdict

    # intent -> handler method name (uniform signature: (frame, ctx, plan)).
    # Adding an intent means adding one row here + the method — no ladder edits.
    _INTENT_HANDLERS = {
        "ratio_analysis": "_h_ratio_analysis",
        "metric_lookup": "_h_metric_lookup",
        "overview": "_h_overview",
        "metric_comparison": "_h_metric_comparison",
        "driver_analysis": "_h_driver_analysis",
        "validation": "_h_validation",
        "edit_history": "_h_edit_history",
        "risk_assessment": "_h_risk_assessment",
        "peer_comparison": "_h_peer_comparison",
        "valuation": "_h_valuation",
        "forecast_validation": "_h_forecast_validation",
        "trend_analysis": "_h_trend",
        "dividend_analysis": "_h_dividends",
        "news_impact": "_h_news",
        "earnings_review": "_h_news",
    }

    def answer(self, query: str, *, audience: str = "analyst",
               history: list[dict] | None = None, now: str | None = None,
               pending_edits: list[dict] | None = None) -> Response:
        _frame, _plan, _ctx, resp = self._run(query, audience, history or [],
                                              now=now, pending_edits=pending_edits or [])
        return resp

    def answer_with_trace(self, query: str, *, audience: str = "analyst",
                          history: list[dict] | None = None, now: str | None = None,
                          pending_edits: list[dict] | None = None) -> tuple[Response, TraceRecord]:
        frame, plan, ctx, resp = self._run(query, audience, history or [],
                                           now=now, pending_edits=pending_edits or [])
        trace = TraceRecord(
            trace_id=ctx.trace_id or self._trace_id(), query=query, audience=audience,
            company=frame.company, frame=frame, plan=plan,
            evidence=ctx.evidence, response=resp,
        )
        return resp, trace

    # ------------------------------------------------------------ core run
    def _run(self, query: str, audience: str, history: list[dict],
             *, now: str | None = None, pending_edits: list[dict] | None = None):
        """Mint a trace id, set up DEBUG observability (a per-query .log file + a
        per-layer artifact dump), then run the pipeline. All dumping is a no-op when
        DEBUG is off — same behavior, ~zero overhead."""
        trace_id = self._trace_id()
        dumper = make_fie_dumper(trace_id)
        with ExitStack() as stack:
            if dumper.enabled:
                stack.enter_context(per_query_log(trace_id))  # logs/<ts>_<id>.log
                dumper.subject(trace_id)
                stack.callback(restore_llms, wrap_llms(self, dumper))  # capture LLM calls
            return self._pipeline(query, audience, trace_id, dumper, history,
                                  now=now, pending_edits=pending_edits or [])

    def _pipeline(self, query: str, audience: str, trace_id: str, dumper,
                  history: list[dict], *, now: str | None = None,
                  pending_edits: list[dict] | None = None):
        t0 = time.monotonic()
        frame = understanding.understand(
            query,
            llm=self.llm,
            available_metrics=list(self.store.available_metrics()),
            available_years=list(self.store.years),
            history=history,
            metric_matcher=self.store.query_metric_matcher(),
        )
        plan = planner.plan(frame, llm=self.llm)
        _layer("Understand", "intent=%s company=%s year=%s formula=%s sources=%s (%s)",
                frame.intent, frame.company, frame.year, frame.formula,
                plan.external_sources, frame.source)
        if dumper.enabled:
            _log.debug("L1 frame=%s", frame.model_dump(), extra={"component": "Understand"})
            dumper.json("01_frame", frame)
            dumper.json("02_plan", plan)

        ctx = _Ctx()
        ctx.trace_id = trace_id
        ctx.dumper = dumper
        ctx.now = now
        ctx.pending_edits = pending_edits or []

        # intent -> handler dispatch (registry, not an if/elif ladder): a new intent
        # without a registered handler degrades EXPLICITLY (logged), never silently.
        handler = self._INTENT_HANDLERS.get(frame.intent)
        if handler is not None:
            getattr(self, handler)(frame, ctx, plan)
        elif frame.intent not in ("unknown", "agent"):
            _layer("Route", "no handler registered for intent=%r; degrading to empty answer",
                   frame.intent)

        # Agentic planner: when the deterministic handler couldn't answer — an `unknown`
        # intent, or a recognized intent that found NOTHING in the workbook — hand off to the
        # LLM agent, which composes deterministic tools (lookups / ratios / growth / decompose
        # / insights / external) to assemble a cited answer. A clarify prompt is left alone.
        # No real LLM (or the agent gathered nothing) -> deterministic external fallback.
        internal_empty = not ctx.evidence and not ctx.calcs and not ctx.selected_insights
        clarifying = bool((ctx.extra or {}).get("clarify"))
        # edit_history answers from the change log (ctx.extra), never from financial evidence —
        # so an empty ctx.evidence is EXPECTED, not a miss. Don't let the agent fallback hijack it.
        handled_via_extra = frame.intent == "edit_history"
        if (not clarifying and not handled_via_extra
                and (frame.intent in ("unknown", "agent") or internal_empty)):
            self._run_agent(frame, ctx)
            if not ctx.evidence and not (ctx.extra or {}).get("agent_answer"):
                self._external_fallback(frame, ctx)  # no-LLM / agent found nothing

        # per-layer dump: intent-stage outputs (evidence / calcs / conflicts / extras)
        if dumper.enabled:
            dumper.json("03_evidence", [e.model_dump() for e in ctx.evidence])
            dumper.json("04_calcs", [c.model_dump() for c in ctx.calcs])
            dumper.json("05_conflicts", [c.model_dump() for c in ctx.conflicts])
            if ctx.extra is not None or ctx.selected_insights:
                dumper.json("06_extra", {"extra": ctx.extra,
                                         "selected_insights": ctx.selected_insights,
                                         "insight_resolutions": ctx.insight_resolutions})

        # corroborate workbook facts with same-period external actuals (analysis_reports)
        # so the cross-source reconciliation below has an overlapping source to compare.
        if frame.intent in ("metric_lookup", "ratio_analysis", "trend_analysis",
                            "peer_comparison"):
            self._corroborate(frame, ctx)

        # admission role (L6): tag every datum so the trust model is explicit —
        # workbook facts = baseline; external = supporting/event/context; news =
        # non-authoritative. External numbers can never be a baseline (admission.py).
        for e in ctx.evidence:
            e.role = admission.classify_evidence(e).value
        # cross-source numeric reconciliation (scale-aware, authority-resolved): a
        # workbook value vs a same-metric external value — fires when an external feed
        # reports a metric the workbook also has (e.g. analysis_reports).
        internal_ev = [e for e in ctx.evidence if e.fact_refs]
        external_ev = [e for e in ctx.evidence if e.kind == "external" and e.value is not None]
        if internal_ev and external_ev:
            ctx.conflicts += self.conflicts.detect_internal_vs_external(internal_ev, external_ev)
        if len(external_ev) > 1:
            # external-vs-external disagreement: surfaced (no trusted baseline to settle it)
            ctx.conflicts += self.conflicts.detect_cross_api(external_ev)

        # per-layer dump: admitted evidence (roles stamped + corroboration merged) + final conflicts
        if dumper.enabled:
            dumper.json("07_evidence_admitted", [e.model_dump() for e in ctx.evidence])
            dumper.json("08_conflicts_final", [c.model_dump() for c in ctx.conflicts])
        # Observability: distinguish what was PLANNED from what was actually fetched. A
        # non-empty external_planned with external=0 means planned sources weren't retrieved.
        external_all = [e for e in ctx.evidence if e.kind == "external"]
        insight_ev = [e for e in ctx.evidence if e.kind == "insight"]
        _layer("Retrieve",
                "evidence=%d (internal=%d insight=%d external=%d) external_planned=%s "
                "calcs=%d conflicts=%d degraded=%s",
                len(ctx.evidence), len(internal_ev), len(insight_ev), len(external_all),
                sorted(plan.external_sources), len(ctx.calcs), len(ctx.conflicts), ctx.degraded)

        cites, withheld = citations_mod.bind(ctx.evidence, ctx.calcs)
        conf = None
        graph = None
        # edit_history is a deterministic listing of app actions (not financial claims): it
        # carries no evidence/calcs, so confidence scoring is meaningless and narration would
        # only risk reformatting timestamps/values past the numeric guard. Render it as-is.
        if frame.intent not in ("unknown", "edit_history"):
            conf = self.confidence.score(
                evidence=ctx.evidence, calcs=ctx.calcs, conflicts=ctx.conflicts,
                selected_insights=ctx.selected_insights,
                degraded=ctx.degraded, partial_coverage=ctx.partial_coverage,
            )
            graph = synthesis.build_graph(frame, ctx.evidence, ctx.calcs, ctx.conflicts)
            # For an AGENT query, prefer the agent's OWN answer over a fresh narration. The
            # agent answer is the product of the tool-calling loop + verification pass (and any
            # deterministic premise correction), grounded in the evidence the agent gathered.
            # narrate() instead re-derives prose from the raw evidence graph and will faithfully
            # re-affirm a FALSE premise the question carried (e.g. "why was gp LESS" → "gp was
            # lower…") — the failure we're closing — and would also discard the verification
            # pass entirely. The numeric guard in response.render still gates whatever we set
            # here (falling back to the deterministic "Compiled N data point(s)" summary if it
            # doesn't verify), so this never surfaces an unbacked figure. Using the agent answer
            # also skips a redundant LLM call. Non-agent intents narrate exactly as before.
            agent_answer = (ctx.extra or {}).get("agent_answer")
            if frame.intent == "agent" and agent_answer:
                ctx.llm_analysis = agent_answer
                _layer("Respond", "agent answer used directly%s (narration skipped)",
                       " [premise-corrected]" if (ctx.extra or {}).get("agent_premise_overridden") else "")
            else:
                ctx.llm_analysis = self.synthesizer.narrate(frame, graph, audience=audience)
        # per-layer dump: confidence (json), reasoning graph (json), narration (text)
        if dumper.enabled:
            if conf is not None:
                dumper.json("09_confidence", conf)
            if graph is not None:
                dumper.json("10_reasoning_graph", graph)
            dumper.text("11_llm_analysis", ctx.llm_analysis or "(no narration)")

        coverage = {
            "degraded": ctx.degraded,
            "partial_coverage": ctx.partial_coverage,
            "dropped_insights": (ctx.total_insights - len(ctx.selected_insights)
                                 if ctx.total_insights else 0),
            "superseded_insights": ctx.superseded,
            "withheld": len(withheld),
            "admission": admission.audit(ctx.evidence),  # role distribution
        }
        if conf and conf.caps_applied:   # decision log: why confidence was capped
            _layer("Confidence", "band=%s limited_by=%s caps=%s",
                    conf.band, getattr(conf, "limited_by", None), conf.caps_applied)
        _layer("Respond", "conf=%s citations=%d coverage=%s",
                (conf.band if conf else "n/a"), len(cites), coverage)
        if dumper.enabled:
            dumper.json("12_citations", {"citations": [c.model_dump() for c in cites],
                                         "withheld": [w.model_dump() for w in withheld]})

        resp = response.render(
            frame, ctx.evidence, ctx.calcs, cites, conf,
            conflicts=ctx.conflicts, withheld=withheld,
            audience=audience, llm_analysis=ctx.llm_analysis, extra=ctx.extra,
            coverage=coverage,
        )
        if dumper.enabled:
            dumper.json("13_response", resp)
            actual_llm = getattr(self.llm, "_llm", self.llm)
            llm_info: dict = {"type": type(actual_llm).__name__}
            if hasattr(actual_llm, "model"):
                llm_info["model"] = actual_llm.model
            if hasattr(actual_llm, "_api_key"):
                llm_info["key_set"] = bool(actual_llm._api_key)
            if hasattr(actual_llm, "last_error") and actual_llm.last_error:
                llm_info["last_error"] = actual_llm.last_error
            dumper.json("00_summary", {
                "trace_id": trace_id, "query": query, "audience": audience,
                "intent": frame.intent, "company": frame.company, "year": frame.year,
                "formula": frame.formula, "confidence": conf.band if conf else None,
                "evidence": len(ctx.evidence), "calcs": len(ctx.calcs),
                "conflicts": len(ctx.conflicts), "citations": len(cites),
                "degraded": ctx.degraded, "partial_coverage": ctx.partial_coverage,
                "frame_source": frame.source,
                "llm": llm_info,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            })
        return frame, plan, ctx, resp

    # ------------------------------------------------------ intent handlers
    # Uniform signature (frame, ctx, plan); registered in _INTENT_HANDLERS.

    def _h_ratio_analysis(self, frame, ctx, plan) -> None:
        # Recover the formula if the intent is ratio_analysis but the id was dropped (e.g. the
        # LLM reclassified "return on equity over the years" to ratio_analysis but left
        # formula=None). Without this the handler no-ops and the renderer used to crash.
        formula = frame.formula
        if not formula:
            m = understanding._matched_formula(frame.raw_query)
            if m:
                formula = frame.formula = m[0]
        if not formula:
            return  # genuinely no ratio to compute -> empty (fallback/renderer handle it)

        # A specific year -> just that year; "for each year / over the years" (no year) ->
        # the ratio for every year that has the needed inputs (a per-year series).
        years = [frame.year] if frame.year is not None else sorted(self.store.years)
        pairs: list[tuple[int, object]] = []
        for y in years:
            try:
                pairs.append((y, self.calc.evaluate(formula, y)))
            except Exception as exc:  # noqa: BLE001 — one bad year shouldn't abort the series
                _log.debug("fie _h_ratio_analysis: evaluate(%s, %s) failed: %s", formula, y, exc,
                           extra={"component": "Calc"})
        valued = [(y, c) for (y, c) in pairs if c is not None and c.value is not None]

        if valued:
            ctx.calcs = [c for (_, c) in valued]
        elif pairs:
            ctx.calcs = [pairs[0][1]]  # keep one failed calc so the renderer can show its note
        else:
            ctx.calcs = []
        ctx.extra = {"ratio_series": [{"year": y, "value": c.value, "unit": c.unit}
                                      for (y, c) in valued]}
        inputs = [f for (_, c) in valued for f in c.inputs]
        ctx.evidence = retrieval.evidence_from_facts(self.store, inputs) if inputs else []
        ctx.conflicts = self.conflicts.detect(
            facts=inputs, report_year_preference=frame.report_year_preference) if inputs else []

    def _h_metric_lookup(self, frame, ctx, plan) -> None:
        # availability-gated resolution + clarification on ambiguous terms
        mr = metric_resolve.resolve(frame.raw_query, frame.metrics,
                                    self.store.available_metrics(),
                                    matcher=self.store.query_metric_matcher())
        if mr["clarify"]:
            ctx.extra = {"clarify": True, "candidates": mr["candidates"],
                         "suggestions": mr["suggestions"]}
            return
        if mr["resolved"] and mr["resolved"] not in (frame.metrics or []):
            frame.metrics = [mr["resolved"]]        # prefer an available canonical
            for req in plan.requirements:
                req.metric = mr["resolved"]
        ctx.extra = {"suggestions": mr["suggestions"], "available": mr["available"]}
        ctx.evidence = retrieval.fetch(self.store, plan)
        ctx.conflicts = self.conflicts.detect(
            facts=[f for e in ctx.evidence for f in e.fact_refs],
            report_year_preference=frame.report_year_preference)
        # "value AND percentage" — a share/percentage request needs the DENOMINATOR too. Intent
        # validation often returns only the numerator metric (e.g. just gross_profit), so the
        # percentage can't be computed. Deterministically fetch the natural base and register the
        # ratio so it's both numerically backed and rendered. No-op for non-percentage lookups.
        if frame.metrics and frame.year and _PCT_REQUEST_RE.search(frame.raw_query or ""):
            self._attach_percentage(frame, ctx)

    def _attach_percentage(self, frame, ctx) -> None:
        """Add the denominator fact + a percentage CalcResult for a 'share of X' metric_lookup.
        Denominator: revenue for P&L items, total_assets for balance-sheet items (with a
        fallback to the other). Primary stays evidence[0] so the headline answer is unchanged."""
        primary, year = frame.metrics[0], frame.year
        try:
            pf = self.store.lookup(primary, year)
        except KeyError:
            return
        if pf.value is None:
            return
        first = "total_assets" if pf.statement == "bs" else "revenue"
        for denom in (first, "revenue" if first == "total_assets" else "total_assets"):
            if denom == primary:
                continue
            try:
                df = self.store.lookup(denom, year)
            except KeyError:
                continue
            if df.value:
                break
        else:
            _log.info("fie metric_lookup: percentage requested but no denominator found for "
                      "%r %s — value only", primary, year, extra={"component": "Respond"})
            return
        pct = round(pf.value / df.value, 6)
        ctx.evidence += retrieval.evidence_from_facts(self.store, [df])  # cite the denominator
        ctx.calcs.append(CalcResult(
            formula_id=f"{primary}_pct_of_{denom}", value=pct, unit="ratio",
            inputs=[pf, df], citations=self.store.cite(df),
            expression=f"{primary} / {denom}", confidence="High"))
        ctx.extra = {**(ctx.extra or {}),
                     "percentage": {"metric": primary, "denom": denom, "pct": pct, "year": year}}
        _log.info("fie metric_lookup: %s = %.2f%% of %s (%s) — denominator fetched + ratio registered",
                  primary, pct * 100, denom, year, extra={"component": "Respond"})

    # Headline KPIs surfaced for an "overview / summarize the financials" request, in
    # priority order; intersected with what the workbook actually has.
    _OVERVIEW_METRICS = ("revenue", "gross_profit", "operating_profit", "profit_before_tax",
                         "pat", "eps", "total_assets", "total_equity", "total_liabilities")

    def _h_overview(self, frame, ctx, plan) -> None:
        avail = set(self.store.available_metrics())
        keys = [m for m in self._OVERVIEW_METRICS if m in avail] or sorted(avail)[:8]
        # latest year that actually HAS data (workbook years include empty forecast years)
        year = frame.year
        if year is None:
            for y in sorted(self.store.years, reverse=True):
                try:
                    rev = self.store.lookup("revenue", y)
                except KeyError:
                    rev = None
                if rev is not None and rev.value is not None:
                    year = y
                    break
            if year is None and self.store.years:
                year = max(self.store.years)
        facts = []
        for m in keys:
            if year is None:
                break
            try:
                f = self.store.lookup(m, year)
            except KeyError:
                f = None
            if f is not None and f.value is not None:
                facts.append(f)
        ctx.evidence = retrieval.evidence_from_facts(self.store, facts) if facts else []
        ctx.extra = {
            "overview_year": year,
            "overview_items": [{"label": (f.metric or f.label).replace("_", " "),
                                "value": f.value, "unit": f.unit} for f in facts],
        }
        ctx.conflicts = self.conflicts.detect(
            facts=facts, report_year_preference=frame.report_year_preference) if facts else []

    # Component line items per total, for "what drove the change" decomposition. Curated
    # (clean accounting structure) and intersected with what the workbook actually has.
    _ASSET_COMPONENTS = (
        "property_plant_equipment", "operating_fixed_assets", "capital_work_in_progress",
        "intangible_assets", "investment_property", "long_term_investments",
        "long_term_deposits", "long_term_loans_and_advances", "right_of_use_assets",
        "deferred_tax_asset", "stock_in_trade", "stores_spares_loose_tools", "trade_debts",
        "loans_and_advances", "deposits_prepayments_other_receivables", "cash_and_bank",
        "short_term_investments",
    )
    _LIABEQ_COMPONENTS = (
        "paid_up_capital", "reserves", "capital_reserves", "revenue_reserves",
        "unappropriated_profit", "long_term_financing", "lease_liabilities",
        "deferred_tax_liability", "trade_payables", "short_term_borrowings",
        "creditors_accrued_other_liabilities", "accrued_liabilities", "contract_liabilities",
        "unclaimed_dividend",
    )
    _DRIVER_COMPONENTS = {
        "total_assets": _ASSET_COMPONENTS,
        "total_equity_and_liabilities": _LIABEQ_COMPONENTS,
        "total_liabilities": _LIABEQ_COMPONENTS,
        "total_equity": ("paid_up_capital", "reserves", "capital_reserves",
                         "revenue_reserves", "unappropriated_profit"),
    }

    def _run_agent(self, frame, ctx) -> None:
        """Hand off to the agentic planner. Re-labels the intent 'agent' so the response
        layer renders + numerically-verifies the composed answer."""
        frame.intent = "agent"
        agent.run(self, frame, ctx)

    def _h_driver_analysis(self, frame, ctx, plan) -> None:
        """Decompose a total's period-over-period change into its component line items and
        rank by absolute change — the top mover 'drove' the change."""
        target = (frame.metrics[0] if frame.metrics else "total_assets")
        avail = set(self.store.available_metrics()) | set(
            self.store.available_metrics(level="detail"))
        components = [m for m in self._DRIVER_COMPONENTS.get(target, ()) if m in avail]
        # the two most recent years for which the TOTAL has values (the change period)
        yrs = [y for y in sorted(self.store.years) if self._safe_lookup(target, y) is not None]
        if len(yrs) < 2 or not components:
            return  # nothing to decompose -> empty (fallback may help)
        y0, y1 = yrs[-2], yrs[-1]
        t0, t1 = self._safe_lookup(target, y0), self._safe_lookup(target, y1)
        total_delta = (t1 - t0) if (t0 is not None and t1 is not None) else None

        drivers = []
        facts = []
        for m in components:
            v0, v1 = self._safe_lookup(m, y0), self._safe_lookup(m, y1)
            if v0 is None or v1 is None:
                continue
            drivers.append({"label": m.replace("_", " "), "delta": v1 - v0,
                            "from": v0, "to": v1})
            for yy in (y0, y1):
                try:
                    f = self.store.lookup(m, yy)
                    if f is not None and f.value is not None:
                        facts.append(f)
                except KeyError:
                    pass
        for yy in (y0, y1):  # cite the total too
            try:
                f = self.store.lookup(target, yy)
                if f is not None and f.value is not None:
                    facts.append(f)
            except KeyError:
                pass
        drivers.sort(key=lambda d: abs(d["delta"]), reverse=True)
        ctx.evidence = retrieval.evidence_from_facts(self.store, facts) if facts else []
        ctx.extra = {"driver": {"target": target.replace("_", " "), "y0": y0, "y1": y1,
                                "total_delta": total_delta, "drivers": drivers[:5]}}
        ctx.conflicts = self.conflicts.detect(
            facts=facts, report_year_preference=frame.report_year_preference) if facts else []

    def _h_metric_comparison(self, frame, ctx, plan) -> None:
        """Compare two of the company's own metrics/series side by side, per year. Each term
        is a level or (when phrased as 'growth/change') a YoY growth series computed directly
        from the workbook. Renders both series so the LLM can contrast them."""
        terms = understanding._comparison_terms(frame.raw_query, self.store.query_metric_matcher())
        # The raw-query splitter only handles explicit connectors ("A vs B"); phrasings like
        # "what changed more: revenue or profit after tax?" don't split, but the LLM already
        # resolved the two metrics into frame.metrics — use those rather than re-parsing.
        if len(terms) < 2 and len(frame.metrics or []) >= 2:
            growth = bool(understanding._GROWTH_RE.search(frame.raw_query)) or bool(
                re.search(r"chang|grew|grow|increas|decreas|\bmore\b|\bless\b|bigger|smaller|"
                          r"faster|slower|movement", frame.raw_query, re.I))
            terms = [(m.replace("_", " "), m, growth) for m in frame.metrics]
        if len(terms) < 2:
            return  # not actually comparable -> empty (agent / external fallback may help)
        years = sorted(self.store.years)
        comparison: list[dict] = []
        facts = []
        for label, metric, growth in terms:
            pts = []
            for y in years:
                cur = self._safe_lookup(metric, y)
                if cur is None:
                    continue
                try:
                    f = self.store.lookup(metric, y)
                    if f is not None and f.value is not None:
                        facts.append(f)
                except KeyError:
                    pass
                if growth:
                    prev = self._safe_lookup(metric, y - 1)
                    if prev not in (None, 0):
                        pts.append({"year": y, "value": round((cur - prev) / prev, 4),
                                    "unit": "percent"})
                else:
                    pts.append({"year": y, "value": cur, "unit": "currency"})
            if pts:
                comparison.append({"label": label.strip(), "metric": metric,
                                   "growth": growth, "points": pts})
        ctx.evidence = retrieval.evidence_from_facts(self.store, facts) if facts else []
        ctx.extra = {"comparison": comparison}
        ctx.conflicts = self.conflicts.detect(
            facts=facts, report_year_preference=frame.report_year_preference) if facts else []

    def _h_risk_assessment(self, frame, ctx, plan) -> None:
        all_insights = self.store.insights(include_review=True)
        ctx.selected_insights, ctx.insight_resolutions = self.insights.select_and_resolve(
            frame, all_insights)
        ctx.evidence = [insights_mod.insight_evidence(r) for r in ctx.selected_insights]
        ctx.conflicts = self.conflicts.detect(
            insight_resolutions=ctx.insight_resolutions,
            insights=ctx.selected_insights)  # + cross-Area semantic (LLM, if present)
        ctx.total_insights = len(all_insights)
        ctx.superseded = sum(len(r["superseded"]) for r in ctx.insight_resolutions)
        # Fetch the external sources the planner attached (news + PSX announcements) so
        # they actually corroborate the qualitative insights instead of being dropped. The
        # main pipeline role-classifies and reconciles this external evidence against the
        # workbook; external numbers are supporting/context, never a baseline. Insights are
        # the primary basis here, so a missing/empty external feed must NOT degrade the answer.
        if plan.external_sources or plan.registry_apis:
            self._fetch_external(frame, ctx, plan, degrade_on_empty=False)
        ctx.extra = {
            "themes": qualitative.assemble_themes(ctx.selected_insights),
            "qual_coverage": qualitative.coverage(ctx.selected_insights),
        }
        if ctx.dumper and ctx.dumper.enabled:
            ctx.dumper.json("06b_insight_selection", {
                "intent": frame.intent,
                "query": frame.raw_query,
                "metrics": frame.metrics,
                "total_pool": ctx.total_insights,
                "min_relevance": 0.5,
                "selected_count": len(ctx.selected_insights),
                "superseded_count": ctx.superseded,
                "conflict_resolutions_count": len(ctx.insight_resolutions),
                "top_insights": [
                    {
                        "insight_id": r.get("insight_id"),
                        "area": r.get("area"),
                        "year": r.get("year"),
                        "source_section": r.get("source_section"),
                        "relevance": r.get("relevance"),
                        "confidence": r.get("confidence"),
                        "takeaway_preview": (r.get("takeaway") or "")[:120],
                    }
                    for r in ctx.selected_insights[:30]
                ],
                "conflict_resolutions": [
                    {
                        "area": res.get("area"),
                        "decision": res.get("decision"),
                        "ambiguous": res.get("ambiguous"),
                        "winner": res.get("winner", {}).get("insight_id"),
                        "superseded": [s.get("insight_id") for s in res.get("superseded", [])],
                        "rationale": res.get("rationale"),
                    }
                    for res in ctx.insight_resolutions
                ],
            })

    def _h_peer_comparison(self, frame, ctx, plan) -> None:
        self._peer_comparison(frame, ctx)

    def _h_valuation(self, frame, ctx, plan) -> None:
        self._valuation(frame, ctx)

    def _h_forecast_validation(self, frame, ctx, plan) -> None:
        self._forecast_validation(frame, ctx)
        # Pull external context (news + query-driven PSX disclosures / analyst reports) so the
        # forecast judgement isn't internal-only. degrade_on_empty=False: a missing external
        # FORECAST already degrades the answer in _forecast_validation; an empty news/PSX feed
        # must not pile a second degrade on top.
        if plan.external_sources or plan.registry_apis:
            self._fetch_external(frame, ctx, plan, degrade_on_empty=False)
        # Dump insight selection details as a separate layer file
        if ctx.dumper and ctx.dumper.enabled and ctx.selected_insights is not None:
            ctx.dumper.json("06b_insight_selection", {
                "intent": frame.intent,
                "query": frame.raw_query,
                "metrics": frame.metrics,
                "total_pool": ctx.total_insights,
                "min_relevance": 0.5,
                "selected_count": len(ctx.selected_insights),
                "conflict_resolutions_count": len(ctx.insight_resolutions),
                "top_insights": [
                    {
                        "insight_id": r.get("insight_id"),
                        "area": r.get("area"),
                        "year": r.get("year"),
                        "source_section": r.get("source_section"),
                        "relevance": r.get("relevance"),
                        "confidence": r.get("confidence"),
                        "takeaway_preview": (r.get("takeaway") or "")[:120],
                    }
                    for r in ctx.selected_insights[:30]
                ],
                "conflict_resolutions": [
                    {
                        "area": res.get("area"),
                        "decision": res.get("decision"),
                        "ambiguous": res.get("ambiguous"),
                        "winner": res.get("winner", {}).get("insight_id"),
                        "superseded": [s.get("insight_id") for s in res.get("superseded", [])],
                        "rationale": res.get("rationale"),
                    }
                    for res in ctx.insight_resolutions
                ],
                "external_sources": plan.external_sources,
                "registry_apis": plan.registry_apis,
            })

    def _h_trend(self, frame, ctx, plan) -> None:
        self._trend(frame, ctx)

    def _h_dividends(self, frame, ctx, plan) -> None:
        self._dividends(frame, ctx)

    def _h_news(self, frame, ctx, plan) -> None:
        self._fetch_external(frame, ctx, plan)

    def _h_validation(self, frame, ctx, plan) -> None:
        """Deterministic statement audit: accounting identities + component footing + anomaly
        scan. SCOPED to what the question asks — "do equity+liabilities equal total assets" runs
        only the identity checks, "do the components foot" only footing, "find anomalies" only the
        scan — while a generic "audit the statements / are the numbers consistent" runs all three.
        Reuses the agent's tools (one source of truth); the response layer renders the breaks and
        the LLM only narrates them. Never affects non-audit queries."""
        ql = (frame.raw_query or "").lower()
        want_anom = bool(re.search(
            r"anomal|outlier|unusual|mis-?extract|deviat|suspicious|extraction error|"
            r"look\w*\s+(wrong|off)", ql))
        want_foot = bool(re.search(
            r"\bfoot|\bcomponents?\b|sums?\s+to|summed\s+to|add(s|ed)?\s+up\s+to|subtotal|line items?", ql))
        want_ident = bool(re.search(
            r"\bbalanced?\b|\bbalances\b|\bequal\b|reconcile|add up\b|"
            r"(equity|liabilit\w*)\s+(and|plus|\+)\s+(liabilit\w*|equity)|assets?\s+(=|equal)", ql))
        # generic audit -> run everything (explicit "audit/validate/consistent", or no specific cue)
        if (re.search(r"\baudit\b|\bvalidate\b|internally consistent|data quality|sanity|"
                      r"are the (numbers|figures|statements)\b", ql)
                or not (want_anom or want_foot or want_ident)):
            want_anom = want_foot = want_ident = True

        scoped_totals = [t for t in agent._STATEMENT_DECOMP if t.replace("_", " ") in ql]
        breaks: list = []
        n_checks = 0
        if want_ident or want_foot:
            # limit footing to the named total(s); suppress footing entirely (bogus total) when
            # only the identity was asked, so check_balance adds only the identity facts.
            args: dict = {}
            if want_foot:
                if scoped_totals:
                    args["totals"] = scoped_totals
            else:
                args["totals"] = ["__nofooting__"]
            balance = agent._t_check_balance(self, frame, ctx, args)
            n_checks = balance.get("checks_run", 0)
            for b in (balance.get("breaks") or []):
                foot = "components foot to total" in b.get("check", "")
                if (foot and want_foot) or (not foot and want_ident):
                    breaks.append(b)
        anomalies: list = []
        if want_anom:
            anomalies = (agent._t_scan_anomalies(self, frame, ctx, {}).get("anomalies") or [])

        scope = {"ident": want_ident, "foot": want_foot, "anom": want_anom}
        _log.info("fie validation: scope=%s checks=%d breaks=%d anomalies=%d",
                  scope, n_checks, len(breaks), len(anomalies), extra={"component": "Validation"})
        if breaks or anomalies:
            _log.debug("fie validation: breaks=%s anomalies=%s", breaks, anomalies,
                       extra={"component": "Validation"})
        ctx.extra = {**(ctx.extra or {}), "validation_report": {
            "balance": {"breaks": breaks, "checks_run": n_checks, "all_ok": not breaks, "scope": scope},
            "anomalies": {"anomalies": anomalies}}}

    def _h_edit_history(self, frame, ctx, plan) -> None:
        """Answer questions about the user's own edits, from the workbook's History log
        (saved changes) merged with the client's pending/unsaved edits. Filters parsed
        deterministically from the query: unsaved-only, this-session, last-N-minutes/hours/days,
        a specific date, a sheet, and a result limit. Purely a listing — no financial evidence,
        so it never touches the numeric/citation/confidence machinery."""
        q = frame.raw_query or ""
        now = _hist_parse_dt(ctx.now) or datetime.now()

        # Merge the saved log (from the uploaded workbook's History sheet — may be empty or hold
        # PRIOR-session rows, since a file can be opened many times) with the client's unsaved
        # edits. Dedupe by (timestamp, sheet, cell): a re-uploaded file already contains rows a
        # stale pending buffer might resend — the saved copy wins so nothing is counted twice.
        entries: list[dict] = []
        seen: set = set()
        for e in (self.store.history or []):                    # saved log (History sheet)
            seen.add((str(e.get("timestamp") or ""), str(e.get("sheet") or ""),
                      str(e.get("cell") or "")))
            entries.append({**e, "_dt": _hist_parse_dt(e.get("timestamp")),
                            "saved": bool(e.get("saved"))})
        for e in (ctx.pending_edits or []):                     # unsaved edits from the client
            sheet = str(e.get("sheet") or "")
            key = (str(e.get("timestamp") or ""), sheet, str(e.get("cell") or ""))
            if key in seen:
                continue                                        # already saved in the file
            seen.add(key)
            entries.append({
                "timestamp": str(e.get("timestamp") or ""), "sheet": sheet,
                "cell": str(e.get("cell") or ""),
                "old": "" if e.get("old") is None else str(e.get("old")),
                "new": "" if e.get("new") is None else str(e.get("new")),
                "saved": False,
                # the app writes the "workbook opened" marker into the unsaved buffer FIRST
                # (persisted on save), so a session marker can arrive here before it's in the
                # file — tag it so it still bounds "this session".
                "event": "session" if sheet.lower() in ("(session)", "session") else None,
                "_dt": _hist_parse_dt(e.get("timestamp")) or now})

        # "this session" boundary = the latest session-open marker from EITHER source. Taking the
        # max makes prior-session markers in a re-uploaded file harmless (the current open is newest).
        starts = [x["_dt"] for x in entries if x.get("event") == "session" and x.get("_dt")]
        open_markers = [x for x in entries if x.get("event") == "session"]   # one per workbook open
        session_start = max(starts) if starts else None
        changes = [x for x in entries if x.get("event") != "session"]   # drop session markers
        # Drop the app-managed "Manually Verified" COLUMN header write (Validation Ledger, new
        # == "Manually Verified") — that's schema the app adds, not an edit the user made.
        changes = [x for x in changes
                   if not (x["sheet"].strip().lower() == "validation ledger"
                           and str(x.get("new", "")).strip().lower() == "manually verified")]

        applied: list[str] = []
        if re.search(r"\bunsaved\b", q, re.I):
            changes = [x for x in changes if not x["saved"]]
            applied.append("unsaved")
        if re.search(r"\b(this|current) session\b", q, re.I):
            if session_start is not None:
                changes = [x for x in changes if x["_dt"] and x["_dt"] >= session_start]
            else:
                # no open-marker known (file had none yet) — only the live unsaved edits are
                # certainly from this session; prior saved rows can't be attributed to it.
                changes = [x for x in changes if not x["saved"]]
            applied.append("this session")
        win = _HIST_WIN_RE.search(q)
        if win:
            n, unit = int(win.group(1)), win.group(2).lower()
            delta = {"min": timedelta(minutes=n), "minute": timedelta(minutes=n),
                     "hour": timedelta(hours=n), "hr": timedelta(hours=n),
                     "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
            cutoff = now - delta
            changes = [x for x in changes if x["_dt"] and x["_dt"] >= cutoff]
            applied.append(f"last {n} {unit}{'s' if n != 1 else ''}")
        qd = _hist_parse_query_date(q)
        if qd:
            changes = [x for x in changes if x["_dt"] and x["_dt"].date() == qd.date()]
            applied.append(qd.strftime("%Y-%m-%d"))
        sheet_q = self._history_sheet_filter(q, changes)
        if sheet_q:
            changes = [x for x in changes if x["sheet"].lower() == sheet_q.lower()]
            applied.append(sheet_q)

        changes.sort(key=lambda x: (x["_dt"] or datetime.min), reverse=True)   # newest first
        fstr = (" (" + ", ".join(applied) + ")") if applied else ""

        # MODE (checked in order; open_count BEFORE aggregate since "how many" matches both):
        #   open_count : "how many times was it opened/loaded" -> count of workbook-open markers
        #   opened     : "when was it opened/loaded"           -> the session-open time
        #   aggregate  : "how many changes / which sheet / most" -> per-sheet change counts
        #   list       : everything else (supports first/oldest and last/N limits)
        open_count = bool(re.search(r"\b(how many|how often|number of|count)\b[^?]*"
                                    r"\b(open|load|upload)\w*", q, re.I))
        asks_opened = bool(re.search(r"\b(when|what time|at what time)\b[^?]*\b(open|load|upload)\w*",
                                     q, re.I))
        aggregate = bool(re.search(
            r"\b(how many|number of|count of|how often|per sheet|by sheet|each sheet|across sheets?)\b"
            r"|\bwhich sheets?\b|\bmost\s+(change|edit|modif|update)", q, re.I))

        eh: dict = {"filters": applied, "total": len(changes),
                    "now": now.isoformat(timespec="seconds"),
                    "session_known": session_start is not None}
        shown_n = 0
        if open_count:
            eh["mode"] = "open_count"
            n = len(open_markers)
            eh["open_count"] = n
            eh["opens"] = [x["timestamp"] for x in
                           sorted(open_markers, key=lambda x: (x["_dt"] or datetime.min), reverse=True)]
            eh["lead"] = (f"This workbook has been opened {n} time(s) in this app."
                          if n else "I don't have a record of this workbook being opened in this app yet.")
        elif asks_opened:
            eh["mode"] = "opened"
            oa = session_start.isoformat(timespec="seconds") if session_start else None
            eh["opened_at"] = oa
            eh["lead"] = (f"This workbook was opened at {oa} (this session)." if oa
                          else "I don't have a record of when this workbook was opened this session.")
        elif aggregate:
            eh["mode"] = "aggregate"
            by: dict[str, int] = {}
            for x in changes:
                s = x.get("sheet")
                if s:
                    by[s] = by.get(s, 0) + 1
            ordered = sorted(by.items(), key=lambda kv: kv[1], reverse=True)
            eh["by_sheet"] = dict(ordered)
            eh["most"] = list(ordered[0]) if ordered else None
            if ordered:
                m = ordered[0]
                eh["lead"] = (f"You made {len(changes)} change(s) across {len(by)} sheet(s){fstr}. "
                              f"Most changes: {m[0]} ({m[1]} change{'s' if m[1] != 1 else ''}).")
            else:
                eh["lead"] = f"No changes recorded{fstr}."
        else:
            eh["mode"] = "list"
            # "first/earliest/oldest" -> the single OLDEST change; otherwise newest-first,
            # honoring "last N" / "last change".
            asks_first = bool(re.search(r"\b(first|earliest|oldest)\b", q, re.I))
            lm = _HIST_LIMIT_RE.search(q)
            if asks_first:
                ordered_changes = list(reversed(changes))   # oldest first
                limit = 1
            else:
                ordered_changes = changes
                limit = max(1, int(lm.group(1))) if lm else (1 if _HIST_ONE_RE.search(q) else 20)
            shown = ordered_changes[:limit]
            shown_n = len(shown)
            items = []
            for x in shown:
                is_verify = x["sheet"].strip().lower() == "validation ledger"
                item = {"timestamp": x["timestamp"], "sheet": x["sheet"], "cell": x["cell"],
                        "old": x["old"], "new": x["new"], "saved": x["saved"],
                        "kind": "verify" if is_verify else "edit"}
                if is_verify:   # map the ledger row to the financial cell it verifies
                    vs, vc = self._mv_verified_ref(x["cell"])
                    item["verified_sheet"], item["verified_cell"] = vs, vc
                items.append(item)
            eh["items"] = items
            eh["shown"] = shown_n
            if not items:
                eh["lead"] = ("No changes have been recorded for this workbook yet."
                              if (eh["total"] == 0 and not applied) else f"No matching changes found{fstr}.")
            elif asks_first and shown_n == 1:
                eh["lead"] = f"Your first change{fstr}:"
            elif shown_n == 1:
                eh["lead"] = f"Your most recent change{fstr}:"
            else:
                eh["lead"] = (f"{len(changes)} change(s){fstr}"
                              + (f"; showing {shown_n}:" if shown_n < len(changes) else ":"))

        ctx.evidence = []
        ctx.extra = {"edit_history": eh}
        _log.info("fie edit_history: mode=%s total=%d shown=%d filters=%s saved_log=%d pending=%d "
                  "session_start=%s now=%s", eh["mode"], len(changes), shown_n, applied,
                  len(self.store.history or []), len(ctx.pending_edits or []),
                  session_start.isoformat(timespec="seconds") if session_start else None,
                  now.isoformat(timespec="seconds"), extra={"component": "Respond"})

    def _mv_verified_ref(self, cell: str):
        """For a 'Manually Verified' checkbox write at Validation Ledger!<col><row>, resolve the
        financial (sheet, cell) that ledger row refers to. Best-effort -> (sheet|None, cell|None)."""
        m = re.search(r"(\d+)\s*$", cell or "")
        df = getattr(self.store, "validation_ledger", None)
        if not m or df is None or getattr(df, "empty", True):
            return None, None
        idx = int(m.group(1)) - 2   # ledger header is row 1 -> first data row (row 2) is df index 0
        if idx < 0 or idx >= len(df):
            return None, None
        def _col(*names):
            for c in df.columns:
                if any(n in str(c).strip().lower() for n in names):
                    return c
            return None
        sc, cc = _col("sheet"), _col("cell")
        row = df.iloc[idx]
        vs = str(row[sc]).strip() if sc is not None and row[sc] is not None else None
        vc = str(row[cc]).strip() if cc is not None and row[cc] is not None else None
        return vs, vc

    def _history_sheet_filter(self, q: str, changes: list[dict]) -> str | None:
        """Resolve a sheet mentioned in the query to an actual edited sheet name. Direct
        substring match first; then statement-family keywords (balance/p&l/cash flow)."""
        ql = q.lower()
        sheets = sorted({x["sheet"] for x in changes if x.get("sheet")})
        for s in sheets:
            if s.lower() in ql:
                return s
        families = (("balance", ("balance",)), ("income", ("p&l", "p and l", "profit", "income")),
                    ("cash", ("cash flow", "cashflow")))
        for _key, cues in families:
            if any(c in ql for c in cues):
                for s in sheets:
                    sl = s.lower()
                    if _key in sl or any(c.split()[0] in sl for c in cues):
                        return s
        return None

    # ------------------------------------------------------------ handlers
    def _store_for(self, company: str | None):
        if company is None or company == self.store.company:
            return self.store
        return self.external.peers.get(company)

    def _safe_lookup(self, metric: str, year: int | None):
        if year is None:
            return None
        try:
            return self.store.lookup(metric, year).value
        except KeyError:
            return None

    def _peer_comparison(self, frame, ctx) -> None:
        rows = []
        for comp in frame.companies:
            st = self._store_for(comp)
            if st is None:
                ctx.partial_coverage = True
                rows.append({"company": comp, "value": None, "note": "no workbook"})
                continue
            if frame.formula and frame.year is not None:
                cr = CalcEngine(st).evaluate(frame.formula, frame.year)
                ctx.evidence += retrieval.evidence_from_facts(st, cr.inputs)
                rows.append({"company": comp, "value": cr.value, "unit": cr.unit})
            elif frame.metrics and frame.year is not None:
                try:
                    f = st.lookup(frame.metrics[0], frame.year)
                    ctx.evidence += retrieval.evidence_from_facts(st, [f])
                    rows.append({"company": comp, "value": f.value, "unit": f.unit})
                except KeyError:
                    ctx.partial_coverage = True
                    rows.append({"company": comp, "value": None})
        ctx.extra = {"peer_rows": rows, "subject": frame.formula or (frame.metrics[0] if frame.metrics else "metric")}

    def _entity_registry(self):
        """Lazy typed-alias registry over the PSX symbols master (verdict-returning)."""
        if self._registry is None and self.external.symbols is not None:
            try:
                self._registry = entity_registry.EntityRegistry.from_symbols(
                    self.external.symbols)
            except Exception as exc:  # symbols fetch/parse failed -> static-map fallback
                # log (not silent): a code bug here would otherwise masquerade as a
                # benign network fallback and mask wrong-ticker binding.
                self._registry = False
                _log.warning("entity registry unavailable (%s: %s); using static ticker map",
                             type(exc).__name__, exc, extra={"component": "Understand"})
        return self._registry or None

    @staticmethod
    def _looks_like_filename(name) -> bool:
        return bool(name) and bool(re.search(r"\.(xlsx|xlsm|xls|csv)$", str(name), re.I))

    def _effective_company(self, frame=None) -> str | None:
        """The real company name for external queries, or None. Never a filename — a
        session workbook whose company couldn't be derived defaults to its filename, and
        searching news/PSX for a '.xlsx' name returns nothing (and wastes the failover)."""
        c = (getattr(frame, "company", None) if frame is not None else None) or self.store.company
        return None if self._looks_like_filename(c) else c

    def _ticker(self, company: str | None) -> str | None:
        """Resolve a company name to a PSX ticker through the entity registry's
        ladder. Only a RESOLVED verdict binds; REVIEW/QUARANTINED do NOT silently
        bind to a wrong symbol (a typo/unknown ticker-shaped token is quarantined).
        Falls back to the static map. The last verdict is stashed for the renderer."""
        name = company or self._effective_company()
        reg = self._entity_registry()
        if reg is not None and name:
            verdict = reg.resolve(name)
            self._last_entity_verdict = verdict
            if verdict.is_resolved and verdict.ticker:
                return verdict.ticker
            # REVIEW/QUARANTINED: do not bind on a low-confidence guess.
            return COMPANY_TICKER.get(name)
        return COMPANY_TICKER.get(name)

    def _corroborate(self, frame, ctx) -> None:
        """Pull same-period external actuals (analysis_reports) for the metrics the
        workbook facts already cover, giving the divergence / scale-reconcile machinery
        an overlapping external source. CORROBORATES only — admission=supporting, so the
        workbook (audited) always wins a genuine divergence (conflicts.py §8.2).

        Company-aware: grouped by the (company, year) on each internal fact, so a peer
        comparison corroborates each peer against its OWN external record. The workbook
        company name is stamped onto the external evidence so the detector matches
        company-for-company, never cross-company."""
        ar = self.external.analysis_reports
        if ar is None:
            return
        # (company, year) -> canonical metrics read for that company/year
        wanted: dict[tuple[str, int], set[str]] = {}
        for e in ctx.evidence:
            for f in e.fact_refs:
                if f.metric and f.value is not None and f.year and f.company:
                    wanted.setdefault((f.company, f.year), set()).add(f.metric)
        for (company, year), metrics in wanted.items():
            ticker = self._ticker(company)
            if ticker is None:
                continue
            res = ar.facts_for(year, symbol=ticker, metrics=sorted(metrics))
            if not res.items:
                continue
            for it in res.items:                     # tie each external datum to the workbook entity
                it.citations[0].locator["company"] = company
            ctx.evidence += res.items
            if res.status == "cached":
                ctx.degraded = True

    def _market_data(self, ticker, ctx):
        """Gather price/eps/pe/market_cap/shares from company_overview (preferred,
        richer) or the PSX quote stub. Returns a dict; appends cited evidence."""
        md: dict = {}
        cites: list = []
        ov = self.external.company_overview
        if ov is not None:
            res = ov.fetch(symbol=ticker)
            if res.items:
                ctx.evidence += res.items
                cites += [c for i in res.items for c in i.citations]
                if res.status == "cached":
                    ctx.degraded = True
                for i in res.items:
                    md[i.citations[0].locator.get("field")] = i.value
                    md.setdefault("_units", {})[i.citations[0].locator.get("field")] = i.unit
        if "price" not in md and self.external.psx is not None:
            q = self.external.psx.quote(ticker)
            if q.items:
                ctx.evidence += q.items
                cites += [c for i in q.items for c in i.citations]
                if q.status == "cached":
                    ctx.degraded = True
                for i in q.items:
                    md[i.citations[0].locator.get("field")] = i.value
                    md.setdefault("_units", {})[i.citations[0].locator.get("field")] = i.unit
        md["_cites"] = cites
        _log.debug(
            "fie _market_data: ticker=%s source=%s price=%s pe=%s market_cap=%s shares=%s "
            "(overview=%s psx=%s)",
            ticker,
            "overview" if ov is not None and md else ("psx" if md else "none"),
            md.get("price"), md.get("pe_ratio"), md.get("market_cap"), md.get("shares"),
            ov is not None, self.external.psx is not None,
            extra={"component": "Valuation"})
        return md

    def _valuation(self, frame, ctx) -> None:
        ticker = self._ticker(frame.company)
        if ticker is None or (self.external.company_overview is None and self.external.psx is None):
            ctx.degraded = True
            ctx.extra = {"valuation": None, "note": "market data unavailable"}
            return
        md = self._market_data(ticker, ctx)
        cites = md.get("_cites", [])
        price = md.get("price")
        if price is None and md.get("market_cap") is None:
            ctx.degraded = True
            ctx.extra = {"valuation": None, "note": "no market data", "ticker": ticker}
            return

        caveats: list[str] = []   # economic-meaning suppressions (surfaced + logged)

        # workbook magnitudes are "Rupees in thousand"; market data may be thousand
        # (overview) or absolute PKR (screener) — normalize both before any ratio.
        wb_unit = "Rupees in thousand"
        units = md.get("_units", {})
        # market cap must carry a DECLARED unit — never assume a default (a missing unit
        # would mis-scale P/B and EV/EBITDA by ~1000x). Absent -> skip market-cap paths.
        mc_unit = units.get("market_cap")
        if md.get("market_cap") is not None and not mc_unit:
            caveats.append("market-cap unit undeclared; market-cap ratios skipped (no scale assumed)")

        # P/E: prefer the page's reported TTM P/E, else price / EPS. Meaningful only for a
        # profitable company — a negative/zero EPS (a loss) yields no P/E (suppressed).
        eps = md.get("eps")
        reported_pe = md.get("pe_ratio")
        pe = None
        if reported_pe is not None and reported_pe > 0:
            pe = reported_pe
        elif price and price > 0 and eps and eps > 0:
            pe = round(price / eps, 4)
        elif eps is not None and eps <= 0:
            caveats.append("P/E not meaningful (EPS <= 0, i.e. a reported loss)")

        # P/B: market cap / total equity (preferred), else price × shares, else price / BVPS.
        pb = None
        equity = self._safe_lookup("total_equity", frame.year)
        bvps = self.calc.evaluate("book_value_per_share", frame.year) if frame.year else None
        if md.get("market_cap") and mc_unit and equity:
            # canonical-PKR on both sides -> correct regardless of market-cap source scale
            ratio = scale.magnitude_ratio(md["market_cap"], mc_unit, equity, wb_unit)
            pb = round(ratio, 4) if ratio is not None else None
        elif price and md.get("shares") and equity:
            # no (unit-declared) market cap: derive it from price × share count (both
            # absolute PKR / count), then normalize equity (thousands) to canonical PKR.
            ratio = scale.magnitude_ratio(price * md["shares"], "PKR", equity, wb_unit)
            pb = round(ratio, 4) if ratio is not None else None
        elif price and bvps and bvps.value:
            # last resort (no market cap, no share count): per-share book value, assumed
            # already in the workbook's PKR/share scale.
            pb = round(price / bvps.value, 4)
            ctx.evidence += retrieval.evidence_from_facts(self.store, bvps.inputs)

        # EV/EBITDA: meaningful only for positive EBITDA. Market cap requires a declared unit.
        ebitda_res = self.calc.evaluate("ebitda", frame.year) if frame.year else None
        ebitda = ebitda_res.value if ebitda_res else None
        market_cap = md.get("market_cap")
        shares = md.get("shares") or self._safe_lookup("shares_outstanding", frame.year)
        cash = self._safe_lookup("cash_and_bank", frame.year)
        ncl = self._safe_lookup("non_current_liabilities", frame.year)
        cl = self._safe_lookup("current_liabilities", frame.year)
        debt = (ncl or 0.0) + (cl or 0.0) if (ncl is not None or cl is not None) else None
        ev = None
        if ebitda is not None and ebitda <= 0:
            caveats.append("EV/EBITDA not meaningful (EBITDA <= 0)")
        elif market_cap and mc_unit and ebitda and ebitda > 0:
            # bring market cap (its own declared scale) and workbook magnitudes to canonical PKR
            mc_c = scale.to_canonical_pkr(market_cap, mc_unit)
            nd_c = scale.to_canonical_pkr((debt or 0.0) - (cash or 0.0), wb_unit)
            eb_c = scale.to_canonical_pkr(ebitda, wb_unit)
            if mc_c is not None and eb_c:
                ev = {"ev_ebitda": round((mc_c + (nd_c or 0.0)) / eb_c, 4)}
        elif price and md.get("shares") and ebitda and ebitda > 0:
            # derive market cap from price × share count (absolute PKR); normalize the
            # workbook magnitudes (EBITDA / debt / cash) to canonical PKR before mixing.
            ev = ev_over_ebitda(
                price, md["shares"],
                scale.to_canonical_pkr(ebitda, wb_unit),
                scale.to_canonical_pkr(debt, wb_unit) if debt is not None else None,
                scale.to_canonical_pkr(cash, wb_unit) if cash is not None else None)

        if pe is not None:
            ctx.calcs.append(CalcResult(formula_id="pe_ratio", value=pe, unit="x",
                                        expression="P/E (TTM, reported) or price / eps",
                                        confidence="Medium", citations=cites))
        if pb is not None:
            ctx.calcs.append(CalcResult(formula_id="pb_ratio", value=pb, unit="x",
                                        expression="price / book_value_per_share",
                                        confidence="Medium", citations=cites + bvps.citations))
        if ev is not None:
            ctx.calcs.append(CalcResult(
                formula_id="ev_ebitda", value=ev["ev_ebitda"], unit="x",
                expression="(market_cap + net_debt) / EBITDA  [net_debt ~= total liabilities - cash]",
                confidence="Medium",
                citations=cites + (ebitda_res.citations if ebitda_res else [])))
        if caveats:
            _layer("Valuation", "suppressed ratios: %s", "; ".join(caveats))
        ctx.extra = {"valuation": pe, "pb": pb, "ev_ebitda": (ev or {}).get("ev_ebitda"),
                     "ticker": ticker, "caveats": caveats}

    @staticmethod
    def _stated_forecast_target(query: str, latest_value):
        """Extract a target the user stated IN THE QUERY (e.g. 'is a 10% revenue growth
        forecast reasonable?') and convert it to an absolute value to validate. A percentage
        is read as a growth rate applied to the latest actual. Returns (value, desc, pct) or
        (None, None, None)."""
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*%", query or "")
        if not m or latest_value is None:
            return None, None, None
        pct = float(m.group(1)) / 100.0
        return latest_value * (1 + pct), f"{pct:.0%} growth", pct

    def _margin_trend(self) -> list[dict]:
        """Per-year gross & operating margins (from the workbook) for years with revenue."""
        out = []
        for y in sorted(self.store.years):
            rev = self._safe_lookup("revenue", y)
            if not rev:
                continue
            gp = self._safe_lookup("gross_profit", y)
            op = self._safe_lookup("operating_profit", y)
            row = {"year": y,
                   "gross_margin": round(gp / rev, 4) if gp is not None else None,
                   "operating_margin": round(op / rev, 4) if op is not None else None}
            if row["gross_margin"] is not None or row["operating_margin"] is not None:
                out.append(row)
        return out

    def _forecast_validation(self, frame, ctx) -> None:
        repo = self.external.forecast
        metric = frame.metrics[0] if frame.metrics else "revenue"
        fyear = frame.year

        # Full historical actual series (the trusted baseline)
        history: list[tuple[int, float]] = []
        history_facts: list = []
        for y in sorted(self.store.years):
            try:
                f = self.store.lookup(metric, y)
            except KeyError:
                continue
            if f.value is not None and f.period_type == "historical":
                history.append((y, f.value))
                history_facts.append(f)
        if history_facts:
            ctx.evidence += retrieval.evidence_from_facts(self.store, history_facts)
        latest = history_facts[-1] if history_facts else None
        latest_val = latest.value if latest else None

        # What to validate: an external forecast on record first, else the target the user
        # stated in the query ("10% revenue growth"). This is the key fix — a stated target
        # is now actually judged instead of punted back to the user.
        fc = repo.get(self.store.company, metric, fyear) if (repo and fyear) else None
        target_val, target_desc, target_pct = self._stated_forecast_target(frame.raw_query, latest_val)
        test_value = fc.value if fc is not None else target_val
        if fc is not None:
            ctx.evidence.append(fc)

        # Qualitative context (always): insights for the LLM's narration.
        all_insights = self.store.insights(include_review=True)
        sel_insights, resolutions = self.insights.select_and_resolve(frame, all_insights)
        if sel_insights:
            ctx.selected_insights = sel_insights
            ctx.insight_resolutions = resolutions
            ctx.total_insights = len(all_insights)
            ctx.evidence += [insights_mod.insight_evidence(r) for r in sel_insights]

        # Volatility-aware validation against the company's own actuals (exclude the forecast
        # year). validate_forecast applies growth-band / trend-break / plausibility rules.
        hist_rules = [(y, v) for (y, v) in history if (fyear is None or y < fyear)]
        validation = (forecast_rules.validate_forecast(hist_rules, test_value)
                      if test_value is not None else None)

        # CAGR + YoY volatility (so the answer doesn't pretend a smooth trend exists)
        cagr = None
        if len(history) >= 2:
            fv, lv, n = history[0][1], history[-1][1], history[-1][0] - history[0][0]
            if fv and fv > 0 and n >= 1:
                cagr = round((lv / fv) ** (1 / n) - 1, 4)
        yoy = [round(history[i][1] / history[i - 1][1] - 1, 4)
               for i in range(1, len(history)) if history[i - 1][1]]
        margins = (self._margin_trend()
                   if metric in ("revenue", "gross_profit", "operating_profit", "pat") else [])

        # Degrade ONLY when there's nothing to assess (no external forecast AND no stated target).
        ctx.degraded = fc is None and target_val is None
        _layer("Forecast", "metric=%s test=%s verdict=%s cagr=%s yoy=%s margins=%d",
               metric, test_value, (validation or {}).get("outcome"),
               f"{cagr:.1%}" if cagr is not None else "n/a", yoy, len(margins))

        ctx.extra = {
            "metric": metric, "year": fyear,
            "forecast": (fc.value if fc is not None else None),
            "stated_target": target_val, "target_desc": target_desc, "target_pct": target_pct,
            "latest_actual": latest_val, "latest_year": (latest.year if latest else None),
            "history_series": [{"year": y, "value": v} for y, v in history],
            "history_cagr": cagr, "history_yoy": yoy,
            "history_span": ((history[0][0], history[-1][0]) if len(history) >= 2 else None),
            "validation": validation, "margins": margins,
            "note": ("external forecast" if fc is not None
                     else ("stated target" if target_val is not None else "no forecast on record")),
        }

    def _trend(self, frame, ctx) -> None:
        metric = frame.metrics[0] if frame.metrics else "revenue"
        # available historical years for this metric (value present)
        avail = []
        for y in sorted(self.store.years):
            try:
                f = self.store.lookup(metric, y)
                if f.value is not None:
                    avail.append((y, f))
            except KeyError:
                continue
        # apply explicit range or window
        if frame.years:
            avail = [(y, f) for y, f in avail if y in frame.years]
        elif frame.window:
            avail = avail[-frame.window:]
        if not avail:
            ctx.partial_coverage = True
            ctx.extra = {"trend_metric": metric, "series": []}
            return
        facts = [f for _, f in avail]
        ctx.evidence = retrieval.evidence_from_facts(self.store, facts)
        ctx.conflicts = self.conflicts.detect(
            facts=facts, report_year_preference=frame.report_year_preference)
        series = [{"year": y, "value": f.value} for y, f in avail]
        first, last = avail[0][1].value, avail[-1][1].value
        n = avail[-1][0] - avail[0][0]
        cagr = round((last / first) ** (1 / n) - 1, 4) if (first and first > 0 and n >= 1) else None

        # aggregation operators over consecutive points (average increase / growth)
        deltas = [b["value"] - a["value"] for a, b in zip(series, series[1:])]
        pct = [(b["value"] - a["value"]) / a["value"]
               for a, b in zip(series, series[1:]) if a["value"]]
        avg_abs_increase = round(sum(deltas) / len(deltas), 2) if deltas else None
        avg_growth_pct = round(sum(pct) / len(pct), 4) if pct else None

        flagged = self.store.data_quality_flags(metric, [y for y, _ in avail])
        if flagged:
            ctx.partial_coverage = True

        ctx.extra = {"trend_metric": metric, "series": series, "cagr": cagr,
                     "span": (avail[0][0], avail[-1][0]),
                     "aggregation": frame.aggregation,
                     "avg_abs_increase": avg_abs_increase,
                     "avg_growth_pct": avg_growth_pct,
                     "flagged_years": flagged}

    def _dividends(self, frame, ctx) -> None:
        adapter = self.external.payouts
        if adapter is None:
            ctx.degraded = True
            ctx.extra = {"payouts": [], "note": "payout data unavailable"}
            return
        ticker = self._ticker(frame.company)
        res = adapter.payouts(symbol=ticker, company=frame.company)
        if res.status == "failed" or not res.items:
            ctx.degraded = True
            ctx.extra = {"payouts": [], "note": res.note or "no payouts"}
            return
        if res.status == "cached":
            ctx.degraded = True
        ctx.evidence += res.items
        ctx.extra = {"payouts": [{"claim": i.claim, "pct": i.value,
                                  "date": i.citations[0].locator.get("date")}
                                 for i in res.items]}

    # external_sources TOKENS this dispatcher fetches directly. PSX disclosures/market data
    # now flow through plan.registry_apis (RegistryFetcher), not tokens. Tokens owned by
    # other handlers (forecast→_forecast_validation, psx→_valuation, company_payouts→
    # _dividends) — and the legacy psx_announcements/secp tokens (now registry-driven) — must
    # NOT trigger the "no fetcher" warning here.
    _FETCH_HERE = frozenset({"news"})
    _FETCH_ELSEWHERE = frozenset({"forecast", "psx", "company_payouts",
                                  "psx_announcements", "secp"})

    def _external_fallback(self, frame, ctx) -> None:
        """Last-resort external lookup when the workbook had nothing for an internal-only
        intent. Builds a query-driven plan (news + a shortlisted PSX subset) and fetches it
        as supporting context so the answer isn't a bare 'not found'. Never degrades the
        answer (degrade_on_empty=False) — this is a best-effort augmentation."""
        from .apis.registry import shortlist
        from .models import SourcePlan
        fetcher = getattr(self.external, "registry_fetcher", None)
        if self.external.news is None and fetcher is None:
            return  # no external adapters configured — nothing to fall back to
        apis = [a.name for a, _ in shortlist(frame.raw_query, intent=frame.intent, top_k=5)]
        fb = SourcePlan(
            external_sources=(["news"] if self.external.news is not None else []),
            registry_apis=(apis if fetcher is not None else []),
        )
        _log.info(
            "fie: workbook lookup empty for intent=%s -> external fallback (news=%s registry=%s)",
            frame.intent, fb.external_sources != [], apis,
            extra={"component": "News"},
        )
        self._fetch_external(frame, ctx, fb, degrade_on_empty=False)

    def _fetch_external(self, frame, ctx, plan, *, degrade_on_empty: bool = True) -> None:
        """Fetch every external source the planner attached to ``plan.external_sources``.

        Any planned source with NO fetcher here and not owned by another handler is logged
        as a WARNING rather than silently dropped — the previous behaviour where a planned
        source appeared in the ``sources=[...]`` line but was never retrieved.

        ``degrade_on_empty`` marks the answer degraded when no source returned anything; pass
        False when external evidence is merely corroborating internal evidence (e.g.
        risk_assessment, whose insights are the primary basis) so an unconfigured news adapter
        doesn't needlessly degrade an answer that already has solid insight evidence.
        """
        sources = set(plan.external_sources)
        company = self._effective_company(frame)  # real company or None (never a filename)
        got_any = False
        # resolve the ticker up front so news/announcements scope to it when known
        ticker = self._ticker(company)

        unmapped = sources - self._FETCH_HERE - self._FETCH_ELSEWHERE
        if unmapped:
            _log.warning(
                "fie _fetch_external: planned source(s) %s have no fetcher and were NOT "
                "retrieved (intent=%s); remove from the plan or add an adapter",
                sorted(unmapped), frame.intent,
                extra={"component": "News"},
            )

        _log.debug(
            "fie _fetch_external: intent=%s company=%r ticker=%r planned_sources=%s",
            frame.intent, company, ticker, sorted(sources),
            extra={"component": "News"},
        )

        if "news" in sources:
            if self.external.news is None:
                _log.warning(
                    "fie _news: 'news' in plan but external.news adapter is None "
                    "(not configured) -> skipped; set NEWS_API_KEY or news adapter config",
                    extra={"component": "News"},
                )
            else:
                _log.debug(
                    "fie _news: fetching news adapter=%s company=%r ticker=%r anchor=%s",
                    type(self.external.news).__name__, company, ticker, self.external.as_of,
                    extra={"component": "News"},
                )
                res = self.external.news.search(company, symbol=ticker,
                                                anchor_date=self.external.as_of)
                _log.debug(
                    "fie _news: news adapter returned status=%s items=%d",
                    res.status, len(res.items),
                    extra={"component": "News"},
                )
                for i, ev in enumerate(res.items[:5]):
                    loc = ev.citations[0].locator if ev.citations else {}
                    _log.debug(
                        "  news_item[%d] source=%r title=%r published=%s",
                        i, loc.get("source"), (ev.claim or "")[:80], loc.get("published_at"),
                        extra={"component": "News"},
                    )
                # chunk -> embed -> rank vs query -> dedup; only the surviving chunks
                # (each still carrying its article source/author/link) reach the LLM.
                query_text = news_retrieval.build_query_text(frame, company)
                _log.debug(
                    "fie _news: news_retrieval query_text=%r",
                    query_text[:120],
                    extra={"component": "News"},
                )
                before = len(ctx.evidence)
                # entity gate: drop provider results that don't mention the company/ticker at
                # all (off-topic noise). Skipped automatically when no distinctive terms exist.
                entity_terms = news_retrieval.entity_terms_for(company, ticker)
                ctx.evidence += news_retrieval.retrieve(
                    res.items, query_text, anchor_date=self.external.as_of,
                    entity_terms=entity_terms)
                _log.debug(
                    "fie _news: news_retrieval added %d chunks to evidence (total evidence now=%d)",
                    len(ctx.evidence) - before, len(ctx.evidence),
                    extra={"component": "News"},
                )
                got_any = got_any or res.status != "failed"
                # Dump news retrieval details
                if ctx.dumper and ctx.dumper.enabled:
                    ctx.dumper.json("06c_news_retrieval", {
                        "query_text": query_text,
                        "adapter": type(self.external.news).__name__,
                        "fetch_status": res.status,
                        "articles_fetched": len(res.items),
                        "chunks_kept": len(ctx.evidence) - before,
                        "articles": [
                            {
                                "source": (ev.citations[0].locator.get("source") if ev.citations else None),
                                "title": (ev.claim or "")[:100],
                                "published_at": (ev.citations[0].locator.get("published_at") if ev.citations else None),
                                "snippet_len": len((ev.citations[0].locator.get("snippet") or "") if ev.citations else ""),
                            }
                            for ev in res.items
                        ],
                    })
        else:
            _log.debug(
                "fie _news: 'news' not in plan sources=%s -> news fetch skipped",
                sorted(sources),
                extra={"component": "News"},
            )

        # PSX disclosures + market/fundamentals: the query-driven subset of the registry
        # catalog (plan.registry_apis), fetched generically via RegistryFetcher. Each API was
        # picked by shortlist()+intent-floor, so this is a relevant subset, not all 17.
        fetcher = getattr(self.external, "registry_fetcher", None)
        if plan.registry_apis:
            if fetcher is None:
                _log.warning(
                    "fie _fetch_external: registry_apis=%s planned but no RegistryFetcher "
                    "configured -> skipped (wire ExternalSources.registry_fetcher)",
                    plan.registry_apis, extra={"component": "News"},
                )
            else:
                from .apis.registry import REGISTRY
                by_name = {a.name: a for a in REGISTRY}
                sector = None
                if ticker and self.external.symbols is not None:
                    try:
                        sector = self.external.symbols.sector_for(ticker)
                    except Exception:  # noqa: BLE001 — sector is best-effort
                        sector = None
                fetch_summary: list[dict] = []
                for name in plan.registry_apis:
                    api = by_name.get(name)
                    if api is None:
                        _log.warning("fie _fetch_external: registry API %r not found", name,
                                     extra={"component": "News"})
                        continue
                    res = fetcher.fetch(api, symbol=ticker,
                                        query=(None if ticker else company),
                                        year=frame.year, sector=sector)
                    before = len(ctx.evidence)
                    ctx.evidence += res.items
                    got_any = got_any or res.status != "failed"
                    _log.debug(
                        "fie _fetch_external: registry %s status=%s +%d items (total=%d)",
                        name, res.status, len(ctx.evidence) - before, len(ctx.evidence),
                        extra={"component": "News"},
                    )
                    fetch_summary.append({"api": name, "status": res.status,
                                          "items": len(res.items)})
                if ctx.dumper and ctx.dumper.enabled:
                    ctx.dumper.json("06d_registry_fetch", {
                        "ticker": ticker, "company": company, "sector": sector,
                        "planned": plan.registry_apis, "results": fetch_summary,
                    })

        if not got_any and degrade_on_empty:
            ctx.degraded = True  # required external source(s) unavailable
            _log.warning(
                "fie _fetch_external: all requested sources failed/unavailable -> degraded=True sources=%s",
                sorted(sources),
                extra={"component": "News"},
            )
        elif not got_any:
            _log.debug(
                "fie _fetch_external: no external evidence (corroborating fetch) sources=%s "
                "-> not degraded; internal evidence stands",
                sorted(sources),
                extra={"component": "News"},
            )


class _Ctx:
    def __init__(self) -> None:
        self.trace_id: str | None = None
        self.evidence: list[EvidenceItem] = []
        self.calcs: list[CalcResult] = []
        self.conflicts = []
        self.selected_insights: list[dict] = []
        self.insight_resolutions: list[dict] = []
        self.llm_analysis = None
        self.degraded = False
        self.partial_coverage = False
        self.extra: dict | None = None
        self.total_insights = 0
        self.superseded = 0
        self.dumper = None  # DebugDumper — set by _pipeline for dump-layer access in handlers
        self.now: str | None = None          # client/server current time (ISO) for edit_history
        self.pending_edits: list[dict] = []   # unsaved edits sent by the client (edit_history)
