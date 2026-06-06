"""FIE orchestrator (Phase 4).

Routes by intent through the deterministic layers, optional LLM, and now external
sources (PSX / News / Forecast) plus peer (multi-workbook) comparison. External
fetches degrade gracefully: on failure the engine proceeds on internal data and
caps confidence (architecture §3.2, §4, §9.2).
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from . import citations as citations_mod
from . import insights as insights_mod
from . import news_retrieval, planner, response, retrieval, scale, synthesis, understanding
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

    def answer(self, query: str, *, audience: str = "analyst") -> Response:
        _frame, _plan, _ctx, resp = self._run(query, audience)
        return resp

    def answer_with_trace(self, query: str, *, audience: str = "analyst"
                          ) -> tuple[Response, TraceRecord]:
        frame, plan, ctx, resp = self._run(query, audience)
        trace = TraceRecord(
            trace_id=self._trace_id(), query=query, audience=audience,
            company=frame.company, frame=frame, plan=plan,
            evidence=ctx.evidence, response=resp,
        )
        return resp, trace

    # ------------------------------------------------------------ core run
    def _run(self, query: str, audience: str):
        frame = understanding.understand(query, llm=self.llm)
        plan = planner.plan(frame, llm=self.llm)
        _layer("Understand", "intent=%s company=%s year=%s formula=%s sources=%s (%s)",
                frame.intent, frame.company, frame.year, frame.formula,
                plan.external_sources, frame.source)

        ctx = _Ctx()

        if frame.intent == "ratio_analysis" and frame.formula and frame.year is not None:
            cr = self.calc.evaluate(frame.formula, frame.year)
            ctx.calcs = [cr]
            ctx.evidence = retrieval.evidence_from_facts(self.store, cr.inputs)
            ctx.conflicts = self.conflicts.detect(facts=cr.inputs)

        elif frame.intent == "metric_lookup":
            ctx.evidence = retrieval.fetch(self.store, plan)
            ctx.conflicts = self.conflicts.detect(
                facts=[f for e in ctx.evidence for f in e.fact_refs])

        elif frame.intent == "risk_assessment":
            all_insights = self.store.insights(include_review=True)
            ctx.selected_insights, ctx.insight_resolutions = self.insights.select_and_resolve(
                frame, all_insights)
            ctx.evidence = [insights_mod.insight_evidence(r) for r in ctx.selected_insights]
            ctx.conflicts = self.conflicts.detect(
                insight_resolutions=ctx.insight_resolutions,
                insights=ctx.selected_insights)  # + cross-Area semantic (LLM, if present)
            ctx.total_insights = len(all_insights)
            ctx.superseded = sum(len(r["superseded"]) for r in ctx.insight_resolutions)

        elif frame.intent == "peer_comparison":
            self._peer_comparison(frame, ctx)

        elif frame.intent == "valuation":
            self._valuation(frame, ctx)

        elif frame.intent == "forecast_validation":
            self._forecast_validation(frame, ctx)

        elif frame.intent == "trend_analysis":
            self._trend(frame, ctx)

        elif frame.intent == "dividend_analysis":
            self._dividends(frame, ctx)

        elif frame.intent in ("news_impact", "earnings_review"):
            self._news(frame, ctx, plan)

        _layer("Retrieve", "evidence=%d calcs=%d conflicts=%d degraded=%s",
                len(ctx.evidence), len(ctx.calcs), len(ctx.conflicts), ctx.degraded)

        cites, withheld = citations_mod.bind(ctx.evidence, ctx.calcs)
        conf = None
        if frame.intent != "unknown":
            conf = self.confidence.score(
                evidence=ctx.evidence, calcs=ctx.calcs, conflicts=ctx.conflicts,
                selected_insights=ctx.selected_insights,
                degraded=ctx.degraded, partial_coverage=ctx.partial_coverage,
            )
            graph = synthesis.build_graph(frame, ctx.evidence, ctx.calcs, ctx.conflicts)
            ctx.llm_analysis = self.synthesizer.narrate(frame, graph, audience=audience)

        coverage = {
            "degraded": ctx.degraded,
            "partial_coverage": ctx.partial_coverage,
            "dropped_insights": (ctx.total_insights - len(ctx.selected_insights)
                                 if ctx.total_insights else 0),
            "superseded_insights": ctx.superseded,
            "withheld": len(withheld),
        }
        _layer("Respond", "conf=%s citations=%d coverage=%s",
                (conf.band if conf else "n/a"), len(cites), coverage)

        resp = response.render(
            frame, ctx.evidence, ctx.calcs, cites, conf,
            conflicts=ctx.conflicts, withheld=withheld,
            audience=audience, llm_analysis=ctx.llm_analysis, extra=ctx.extra,
            coverage=coverage,
        )
        return frame, plan, ctx, resp

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

    def _ticker(self, company: str | None) -> str | None:
        """Resolve a company name to a PSX ticker: symbols registry first
        (any listed company), then the static fallback map."""
        name = company or self.store.company
        if self.external.symbols is not None:
            t = self.external.symbols.ticker_for(name)
            if t:
                return t
        return COMPANY_TICKER.get(name)

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

        # P/E: prefer the page's reported TTM P/E, else price / EPS
        eps = md.get("eps")
        pe = md.get("pe_ratio") or (round(price / eps, 4) if (price and eps) else None)

        # workbook magnitudes are "Rupees in thousand"; market data may be thousand
        # (overview) or absolute PKR (screener) — normalize both before any ratio.
        wb_unit = "Rupees in thousand"
        units = md.get("_units", {})

        # P/B: market cap / total equity (preferred, both available), else price / BVPS
        pb = None
        equity = self._safe_lookup("total_equity", frame.year)
        bvps = self.calc.evaluate("book_value_per_share", frame.year) if frame.year else None
        if md.get("market_cap") and equity:
            # canonical-PKR on both sides -> correct regardless of market-cap source scale
            ratio = scale.magnitude_ratio(md["market_cap"], units.get("market_cap") or wb_unit,
                                          equity, wb_unit)
            pb = round(ratio, 4) if ratio is not None else None
        elif price and bvps and bvps.value:
            pb = round(price / bvps.value, 4)
            ctx.evidence += retrieval.evidence_from_facts(self.store, bvps.inputs)

        # EV/EBITDA: market cap from the overview (× shares if only price), net debt
        # proxy + internal EBITDA. Market cap/shares now come from the page.
        ebitda_res = self.calc.evaluate("ebitda", frame.year) if frame.year else None
        ebitda = ebitda_res.value if ebitda_res else None
        market_cap = md.get("market_cap")
        shares = md.get("shares") or self._safe_lookup("shares_outstanding", frame.year)
        cash = self._safe_lookup("cash_and_bank", frame.year)
        ncl = self._safe_lookup("non_current_liabilities", frame.year)
        cl = self._safe_lookup("current_liabilities", frame.year)
        debt = (ncl or 0.0) + (cl or 0.0) if (ncl is not None or cl is not None) else None
        ev = None
        if market_cap and ebitda:
            # bring market cap (its own scale) and the workbook magnitudes to canonical PKR
            mc_c = scale.to_canonical_pkr(market_cap, units.get("market_cap") or wb_unit)
            nd_c = scale.to_canonical_pkr((debt or 0.0) - (cash or 0.0), wb_unit)
            eb_c = scale.to_canonical_pkr(ebitda, wb_unit)
            if mc_c is not None and eb_c:
                ev = {"ev_ebitda": round((mc_c + (nd_c or 0.0)) / eb_c, 4)}
        elif price and shares and ebitda:
            ev = ev_over_ebitda(price, shares, ebitda, debt, cash)

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
        ctx.extra = {"valuation": pe, "pb": pb, "ev_ebitda": (ev or {}).get("ev_ebitda"),
                     "ticker": ticker}

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
        ctx.extra = {"forecast": fc.value, "metric": metric, "year": fyear,
                     "latest_actual": (latest.value if latest else None),
                     "latest_year": (latest.year if latest else None)}

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
        ctx.conflicts = self.conflicts.detect(facts=facts)
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
