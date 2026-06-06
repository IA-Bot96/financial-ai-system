"""FIE orchestrator (Phase 4).

Routes by intent through the deterministic layers, optional LLM, and now external
sources (PSX / News / Forecast) plus peer (multi-workbook) comparison. External
fetches degrade gracefully: on failure the engine proceeds on internal data and
caps confidence (architecture §3.2, §4, §9.2).
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import ExitStack
from typing import Callable

from app.core.debug import make_fie_dumper
from app.core.logging import per_query_log
from . import citations as citations_mod
from . import insights as insights_mod
from .debug_dump import restore_llms, wrap_llms
from . import (admission, entity_registry, forecast_rules, metric_resolve,
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
        "risk_assessment": "_h_risk_assessment",
        "peer_comparison": "_h_peer_comparison",
        "valuation": "_h_valuation",
        "forecast_validation": "_h_forecast_validation",
        "trend_analysis": "_h_trend",
        "dividend_analysis": "_h_dividends",
        "news_impact": "_h_news",
        "earnings_review": "_h_news",
    }

    def answer(self, query: str, *, audience: str = "analyst") -> Response:
        _frame, _plan, _ctx, resp = self._run(query, audience)
        return resp

    def answer_with_trace(self, query: str, *, audience: str = "analyst"
                          ) -> tuple[Response, TraceRecord]:
        frame, plan, ctx, resp = self._run(query, audience)
        trace = TraceRecord(
            trace_id=ctx.trace_id or self._trace_id(), query=query, audience=audience,
            company=frame.company, frame=frame, plan=plan,
            evidence=ctx.evidence, response=resp,
        )
        return resp, trace

    # ------------------------------------------------------------ core run
    def _run(self, query: str, audience: str):
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
            return self._pipeline(query, audience, trace_id, dumper)

    def _pipeline(self, query: str, audience: str, trace_id: str, dumper):
        t0 = time.monotonic()
        frame = understanding.understand(query, llm=self.llm)
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

        # intent -> handler dispatch (registry, not an if/elif ladder): a new intent
        # without a registered handler degrades EXPLICITLY (logged), never silently.
        handler = self._INTENT_HANDLERS.get(frame.intent)
        if handler is not None:
            getattr(self, handler)(frame, ctx, plan)
        elif frame.intent != "unknown":
            _layer("Route", "no handler registered for intent=%r; degrading to empty answer",
                   frame.intent)

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
        _layer("Retrieve", "evidence=%d calcs=%d conflicts=%d degraded=%s",
                len(ctx.evidence), len(ctx.calcs), len(ctx.conflicts), ctx.degraded)

        cites, withheld = citations_mod.bind(ctx.evidence, ctx.calcs)
        conf = None
        graph = None
        if frame.intent != "unknown":
            conf = self.confidence.score(
                evidence=ctx.evidence, calcs=ctx.calcs, conflicts=ctx.conflicts,
                selected_insights=ctx.selected_insights,
                degraded=ctx.degraded, partial_coverage=ctx.partial_coverage,
            )
            graph = synthesis.build_graph(frame, ctx.evidence, ctx.calcs, ctx.conflicts)
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
            dumper.json("00_summary", {
                "trace_id": trace_id, "query": query, "audience": audience,
                "intent": frame.intent, "company": frame.company, "year": frame.year,
                "formula": frame.formula, "confidence": conf.band if conf else None,
                "evidence": len(ctx.evidence), "calcs": len(ctx.calcs),
                "conflicts": len(ctx.conflicts), "citations": len(cites),
                "degraded": ctx.degraded, "partial_coverage": ctx.partial_coverage,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            })
        return frame, plan, ctx, resp

    # ------------------------------------------------------ intent handlers
    # Uniform signature (frame, ctx, plan); registered in _INTENT_HANDLERS.

    def _h_ratio_analysis(self, frame, ctx, plan) -> None:
        if not (frame.formula and frame.year is not None):
            return                                  # nothing to compute -> empty (as before)
        cr = self.calc.evaluate(frame.formula, frame.year)
        ctx.calcs = [cr]
        ctx.evidence = retrieval.evidence_from_facts(self.store, cr.inputs)
        ctx.conflicts = self.conflicts.detect(
            facts=cr.inputs, report_year_preference=frame.report_year_preference)

    def _h_metric_lookup(self, frame, ctx, plan) -> None:
        # availability-gated resolution + clarification on ambiguous terms
        mr = metric_resolve.resolve(frame.raw_query, frame.metrics,
                                    self.store.available_metrics())
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
        ctx.extra = {
            "themes": qualitative.assemble_themes(ctx.selected_insights),
            "qual_coverage": qualitative.coverage(ctx.selected_insights),
        }

    def _h_peer_comparison(self, frame, ctx, plan) -> None:
        self._peer_comparison(frame, ctx)

    def _h_valuation(self, frame, ctx, plan) -> None:
        self._valuation(frame, ctx)

    def _h_forecast_validation(self, frame, ctx, plan) -> None:
        self._forecast_validation(frame, ctx)

    def _h_trend(self, frame, ctx, plan) -> None:
        self._trend(frame, ctx)

    def _h_dividends(self, frame, ctx, plan) -> None:
        self._dividends(frame, ctx)

    def _h_news(self, frame, ctx, plan) -> None:
        self._news(frame, ctx, plan)

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

    def _ticker(self, company: str | None) -> str | None:
        """Resolve a company name to a PSX ticker through the entity registry's
        ladder. Only a RESOLVED verdict binds; REVIEW/QUARANTINED do NOT silently
        bind to a wrong symbol (a typo/unknown ticker-shaped token is quarantined).
        Falls back to the static map. The last verdict is stashed for the renderer."""
        name = company or self.store.company
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

    def _forecast_validation(self, frame, ctx) -> None:
        repo = self.external.forecast
        metric = frame.metrics[0] if frame.metrics else "revenue"
        fyear = frame.year

        # always gather the internal latest actual (this is the degrade-to-internal path)
        latest = None
        for y in sorted(self.store.years, reverse=True):
            try:
                f = self.store.lookup(metric, y)
                if f.value is not None:
                    latest = f
                    break
            except KeyError:
                continue
        if latest is not None:
            ctx.evidence += retrieval.evidence_from_facts(self.store, [latest])

        fc = repo.get(self.store.company, metric, fyear) if (repo and fyear) else None
        if fc is None:
            ctx.degraded = True  # forecast source unavailable -> proceed on internal only
            ctx.extra = {"forecast": None, "metric": metric, "year": fyear,
                         "latest_actual": (latest.value if latest else None),
                         "latest_year": (latest.year if latest else None),
                         "note": "no forecast on record"}
            return
        ctx.evidence.append(fc)
        # validate the forecast against the company's OWN historical actual series
        # (workbook = trusted baseline): volatility-adaptive growth / trend / scale rules.
        history: list[tuple[int, float]] = []
        for y in sorted(self.store.years):
            if fyear and y >= fyear:
                continue
            try:
                f = self.store.lookup(metric, y)
            except KeyError:
                continue
            if f.value is not None and f.period_type == "historical":
                history.append((y, f.value))
        validation = forecast_rules.validate_forecast(history, fc.value)
        ctx.extra = {"forecast": fc.value, "metric": metric, "year": fyear,
                     "latest_actual": (latest.value if latest else None),
                     "latest_year": (latest.year if latest else None),
                     "validation": validation}

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

    def _news(self, frame, ctx, plan) -> None:
        sources = set(plan.external_sources)
        company = frame.company or self.store.company
        got_any = False
        # resolve the ticker up front so news/announcements scope to it when known
        ticker = self._ticker(frame.company)

        if "news" in sources and self.external.news is not None:
            # query-relevant: scope to the ticker if resolved, else keyword on company
            res = self.external.news.search(company, symbol=ticker,
                                            anchor_date=self.external.as_of)
            # chunk -> embed -> rank vs query -> dedup; only the surviving chunks
            # (each still carrying its article source/author/link) reach the LLM.
            query_text = news_retrieval.build_query_text(frame, company)
            ctx.evidence += news_retrieval.retrieve(
                res.items, query_text, anchor_date=self.external.as_of)
            got_any = got_any or res.status != "failed"
        # PSX company announcements (POST form, date-windowed) per the source plan;
        # prefer the resolved ticker, fall back to a company keyword query.
        if "psx_announcements" in sources and self.external.announcements is not None:
            res = self.external.announcements.recent(
                query=None if ticker else company, symbol=ticker,
                anchor_date=self.external.as_of)
            ctx.evidence += res.items
            got_any = got_any or res.status != "failed"
        if "secp" in sources and self.external.secp is not None:
            res = self.external.secp.recent(
                query=None if ticker else company, symbol=ticker,
                anchor_date=self.external.as_of)
            ctx.evidence += res.items
            got_any = got_any or res.status != "failed"

        if not got_any:
            ctx.degraded = True  # required external source(s) unavailable


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
