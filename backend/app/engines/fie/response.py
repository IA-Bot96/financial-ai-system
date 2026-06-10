"""Response generation (L8b) — Phase 2 deterministic renderer.

Assembles the 7-section structure from structured inputs. No LLM prose yet
(Phase 3 adds it, constrained to these same facts). Every figure shown traces
to a Citation or is omitted/withheld.

See docs/fie_implementation_plan.md §Phase 1 (1.7) / §Phase 2.
"""

from __future__ import annotations

import logging

from . import citation_enforce, divergence, evidence_rank, safety
from .models import (
    CalcResult,
    Citation,
    Conflict,
    ConfidenceReport,
    EvidenceItem,
    FactRef,
    QueryFrame,
    Response,
)


_log = logging.getLogger("app.engines.fie")


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit in ("ratio", "x"):
        return f"{value:.2f}x"
    if unit == "percent":
        return f"{value:.1%}"
    return f"{value:,.0f}"


def _cite_of(ev: EvidenceItem) -> str:
    return ev.citations[0].ref_id if ev.citations else "—"


def render(
    frame: QueryFrame,
    evidence: list[EvidenceItem],
    calcs: list[CalcResult],
    citations: list[Citation],
    confidence: ConfidenceReport | None,
    *,
    conflicts: list[Conflict] | None = None,
    withheld: list[FactRef] | None = None,
    audience: str = "analyst",
    llm_analysis: str | None = None,
    extra: dict | None = None,
    coverage: dict | None = None,
) -> Response:
    conflicts = conflicts or []
    withheld = withheld or []
    extra = extra or {}
    company = frame.company or "The company"

    # proactive citation check (L8a): evidence carrying a VALUE but no resolvable citation
    # would become an uncitable claim (dropped later by enforce_findings). Surfacing it
    # here makes a downstream INSUFFICIENT_EVIDENCE explainable instead of mysterious.
    uncitable = [e for e in evidence if e.value is not None and not e.citations]
    if uncitable:
        _log.warning("%d valued evidence item(s) lack a citation (intent=%s): %s",
                     len(uncitable), frame.intent,
                     [str(e.claim)[:60] for e in uncitable][:3],
                     extra={"component": "Respond"})

    findings: list[str] = []
    analysis = ""
    edit_history_payload: dict | None = None   # structured edit-history for the UI (set below)
    primary = calcs[0] if calcs else None

    if frame.intent == "ratio_analysis":
        # formula may be None if it couldn't be resolved — never crash on .replace().
        label = (frame.formula or "the requested ratio").replace("_", " ")
        series = (extra or {}).get("ratio_series") or []  # [{year,value,unit}] per-year
        if series:
            if len(series) == 1:
                s = series[0]
                direct = f"{company}'s {label} for {s['year']} is {_fmt(s['value'], s.get('unit'))}."
                if primary and primary.expression:
                    analysis = (f"Computed as {primary.expression}. Inputs drawn from the "
                                f"workbook (authoritative) and cited below.")
            else:
                parts = ", ".join(f"{s['year']}: {_fmt(s['value'], s.get('unit'))}" for s in series)
                direct = f"{company}'s {label} by year — {parts}."
                analysis = ("Computed per year from workbook inputs (authoritative); "
                            "see citations.")
        else:
            note = primary.note if primary else "insufficient data"
            yr = f" for {frame.year}" if frame.year else ""
            direct = f"Unable to compute {label}{yr}: {note}."
        findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]

    elif frame.intent == "metric_lookup":
        if extra.get("clarify"):
            cands = ", ".join(c.replace("_", " ") for c in extra.get("candidates", []))
            direct = (f'Your question ("{frame.raw_query}") is ambiguous — did you mean: '
                      f"{cands}? Please specify which.")
            findings = []
        else:
            valued = [e for e in evidence if e.value is not None]
            metric = frame.metrics[0].replace("_", " ") if frame.metrics else "metric"
            if valued:
                e = valued[0]
                direct = f"{company}'s {metric} for {frame.year} was {_fmt(e.value, 'currency')} (Rs '000)."
                # share/percentage request: append the deterministically-computed ratio (the
                # denominator fact + CalcResult were attached in _attach_percentage) so the
                # user's "and percentage" is answered even if the LLM narration omits it.
                pct = (extra or {}).get("percentage")
                if pct and pct.get("pct") is not None:
                    direct += f" That is {pct['pct'] * 100:.2f}% of {pct['denom'].replace('_', ' ')}."
            else:
                sugg = extra.get("suggestions") or []
                hint = (" Available metrics include: "
                        + ", ".join(s.replace('_', ' ') for s in sugg[:8]) + "."
                        ) if sugg else ""
                direct = f"{company}'s {metric} for {frame.year} was not found in the workbook.{hint}"
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in valued]

    elif frame.intent == "overview":
        items = (extra or {}).get("overview_items") or []
        yr = (extra or {}).get("overview_year")
        if items:
            listed = "; ".join(f"{it['label']} {_fmt(it['value'], it.get('unit'))}" for it in items)
            direct = (f"{company}'s key financials" + (f" for {yr}" if yr else "")
                      + f": {listed}.")
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
        else:
            direct = "No headline financial metrics were found in the workbook to summarize."
            findings = []

    elif frame.intent == "driver_analysis":
        d = (extra or {}).get("driver") or {}
        drivers = d.get("drivers") or []
        if drivers:
            top = drivers[0]
            td = d.get("total_delta")
            arrow = "increase" if top["delta"] >= 0 else "decrease"
            base = (f"{top['label'].title()} had the largest single change among {d['target']} "
                    f"line items between FY{d['y0']} and FY{d['y1']}: a {_fmt(top['delta'], 'currency')} "
                    f"{arrow} ({_fmt(top['from'], 'currency')} → {_fmt(top['to'], 'currency')})")
            # Only express a "% of the net change" when the net change is material relative to
            # the mover — otherwise large offsetting line-item moves make that ratio nonsensical.
            if td not in (None, 0) and abs(td) >= 0.5 * abs(top["delta"]):
                direct = base + f", ~{abs(top['delta'] / td):.0%} of the {_fmt(td, 'currency')} net change in {d['target']}."
            elif td is not None:
                direct = (base + f". Net {d['target']} barely moved ({_fmt(td, 'currency')}) — "
                          f"large line-item moves largely offset each other.")
            else:
                direct = base + "."
            ranked = "; ".join(f"{x['label']} Δ{_fmt(x['delta'], 'currency')}" for x in drivers)
            analysis = f"Top movers FY{d['y0']}→FY{d['y1']}: {ranked}."
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
        else:
            direct = f"Couldn't decompose the change in {d.get('target', 'the total')} from the workbook."
            findings = []

    elif frame.intent == "metric_comparison":
        comp = (extra or {}).get("comparison") or []
        resolved = [c for c in comp if c.get("points")]
        if len(resolved) >= 2:
            lines = []
            for c in resolved:
                ser = ", ".join(f"{p['year']}: {_fmt(p['value'], p.get('unit'))}" for p in c["points"])
                lines.append(f"{c['label']} — {ser}")
            direct = f"{company} — comparison by year. " + " | ".join(lines)
        elif resolved:
            c = resolved[0]
            ser = ", ".join(f"{p['year']}: {_fmt(p['value'], p.get('unit'))}" for p in c["points"])
            direct = (f"Only one of the requested series resolved from the workbook — "
                      f"{c['label']} — {ser}.")
        else:
            direct = "Couldn't resolve the metrics to compare from the workbook."
        # findings carry citation refs (the underlying facts) so citation-enforcement keeps
        # them; the per-year comparison itself lives in `direct` above.
        findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]

    elif frame.intent == "agent":
        # The agentic planner composed deterministic tools. Its prose answer lives on
        # llm_analysis and is promoted to `direct` below ONLY if it passes the numeric guard;
        # this is the safe deterministic fallback shown if it doesn't (so an unverified
        # figure is never surfaced).
        valued = [e for e in evidence if e.value is not None]
        findings = [f"{e.claim} [{_cite_of(e)}]" for e in valued][:12]
        if valued:
            direct = f"Compiled {len(valued)} workbook data point(s) for your question — see findings."
        elif evidence:
            direct = f"Gathered {len(evidence)} item(s) of context for your question — see findings."
        else:
            direct = "I couldn't find data to answer that — try a specific metric or rephrasing."

    elif frame.intent == "validation":
        rep = (extra or {}).get("validation_report") or {}
        bal = rep.get("balance") or {}
        ano = rep.get("anomalies") or {}
        breaks = bal.get("breaks") or []
        anomalies = ano.get("anomalies") or []
        n_checks = bal.get("checks_run", 0)
        # Audit findings are derived from the cited workbook facts the tools added — tag each
        # with the citation of the SPECIFIC (metric, year) fact it concerns (keyed off the
        # fact_refs, NOT the claim text), so multi-year breaks don't all collapse onto one handle.
        ref_by: dict[tuple, str] = {}
        any_ref = None
        for e in evidence:
            if not e.citations:
                continue
            h = _cite_of(e)
            any_ref = any_ref or h
            for fr in e.fact_refs:
                if fr.metric and fr.year is not None:
                    ref_by.setdefault((fr.metric, int(fr.year)), h)

        def _vref(year, *metrics):
            for m in metrics:
                if m is not None and (m, year) in ref_by:
                    return ref_by[(m, year)]
            return any_ref

        def _tag(text: str, ref):
            return f"{text} [{ref}]" if ref else text

        for b in breaks:
            yr = b["year"]
            if "summed" in b:
                total = b["check"].split()[0]  # e.g. "gross_profit components foot..." -> gross_profit
                findings.append(_tag(
                    f"{b['check']} ({yr}): components sum to {_fmt(b['summed'], 'currency')} "
                    f"vs stated {_fmt(b['stated'], 'currency')} — off by {_fmt(b['variance'], 'currency')}.",
                    _vref(yr, total)))
            else:
                findings.append(_tag(
                    f"{b['check']} ({yr}): {_fmt(b['lhs'], 'currency')} vs "
                    f"{_fmt(b['rhs'], 'currency')} — off by {_fmt(b['variance'], 'currency')}.",
                    _vref(yr, "total_assets", "total_equity_and_liabilities")))
        for a in anomalies:
            findings.append(_tag(
                f"{a['metric'].replace('_', ' ')} {a['year']} = {_fmt(a['value'], 'currency')} looks "
                f"anomalous (~{a['ratio']}x its neighbours) — possible extraction error, worth verifying.",
                _vref(a["year"], a["metric"])))
        # word the summary to the SCOPE actually run (don't claim "no anomalies" if not scanned)
        scope = bal.get("scope") or {"ident": True, "foot": True, "anom": True}
        if breaks or anomalies:
            parts = []
            if (scope["ident"] or scope["foot"]):
                parts.append(f"{len(breaks)} reconciliation break(s)")
            if scope["anom"]:
                parts.append(f"{len(anomalies)} anomaly(ies)")
            across = f" across {n_checks} check(s)" if n_checks else ""
            direct = "Audit found " + " and ".join(parts) + across + " — see findings."
        else:
            oks = []
            if (scope["ident"] or scope["foot"]) and n_checks:
                oks.append("the statements foot and balance")
            if scope["anom"]:
                oks.append("no figures look anomalous")
            if oks:
                direct = ((f"Audited {n_checks} check(s): " if n_checks else "")
                          + ", and ".join(oks) + ".")
                if not n_checks:  # anomaly-only, nothing flagged
                    direct = "No figures look anomalous."
            else:
                direct = "Not enough balance-sheet detail in this workbook to run the audit checks."

    elif frame.intent == "risk_assessment":
        themes = extra.get("themes") or []
        qcov = extra.get("qual_coverage") or {}
        if themes:
            # theme-based view: group insights into taxonomy themes ranked by materiality,
            # each cited to a representative insight; surface coverage gaps + divergence.
            ref_by_insight = {e.citations[0].locator.get("insight_id"): _cite_of(e)
                              for e in evidence if e.citations}
            n_cat = len({t["category_ref"] for t in themes})
            direct = (f"Key risks & qualitative themes for {company}: {len(themes)} theme(s) "
                      f"across {n_cat} categor{'y' if n_cat == 1 else 'ies'}"
                      + (f" (FY{frame.year})" if frame.year else "")
                      + f"; coverage {qcov.get('run_status', 'n/a').lower()}.")
            for t in themes[:8]:
                ref = next((ref_by_insight.get(i) for i in t["insight_ids"]
                            if ref_by_insight.get(i)), None) or "—"
                tag = " — divergent views" if t.get("divergent") else ""
                findings.append(
                    f"{t['category_name']}: {t['theme_name']} "
                    f"(materiality {t['materiality']:.2f}, {t['signal_count']} signal(s)){tag} [{ref}]")
            weak = sorted(c for c, v in qcov.get("categories", {}).items()
                          if v["status"].startswith("SKIPPED") or v.get("expected_section_absent"))
            if weak:
                analysis = ("Coverage caveat: " + ", ".join(weak)
                            + " — limited signals or expected report sections not read.")
        else:
            direct = (f"Identified {len(evidence)} risk-related insight(s) for {company}"
                      + (f" ({frame.year})." if frame.year else "."))
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence_rank.top(evidence, 8)]
        if conflicts and not analysis:
            analysis = (f"{len(conflicts)} insight conflict(s) resolved by recency/"
                        f"confidence; superseded views retained as caveats.")

    elif frame.intent == "peer_comparison":
        rows = extra.get("peer_rows", [])
        subj = (extra.get("subject") or "metric").replace("_", " ")
        unit = next((r.get("unit") for r in rows if r.get("value") is not None), "currency")
        parts = []
        for r in rows:
            parts.append(f"{r['company']}: "
                         + (_fmt(r["value"], unit) if r.get("value") is not None else "n/a"))
        direct = f"{subj.title()} comparison ({frame.year}) — " + "; ".join(parts) + "."
        findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None][:8]

    elif frame.intent == "valuation":
        pe, pb, ev = extra.get("valuation"), extra.get("pb"), extra.get("ev_ebitda")
        parts = []
        if pe is not None:
            parts.append(f"P/E {_fmt(pe, 'x')}")
        if pb is not None:
            parts.append(f"P/B {_fmt(pb, 'x')}")
        if ev is not None:
            parts.append(f"EV/EBITDA {_fmt(ev, 'x')}")
        caveats = extra.get("caveats") or []
        if parts:
            direct = f"{company}'s valuation ({extra.get('ticker')}): " + "; ".join(parts) + "."
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence]
        else:
            direct = (f"Unable to value {company}: {extra.get('note', 'market data unavailable')}. "
                      f"Internal financials remain available.")
        if caveats:   # surface suppressed-ratio reasons (negative EPS/EBITDA, missing unit)
            analysis = ("Not reported: " + "; ".join(caveats) + ".") + (f" {analysis}" if analysis else "")

    elif frame.intent == "forecast_validation":
        fc = extra.get("forecast")
        target = extra.get("stated_target")
        act = extra.get("latest_actual")
        metric = (extra.get("metric") or "metric").replace("_", " ")
        val = extra.get("validation") or {}
        verdict = val.get("outcome")
        implied = val.get("implied_growth")
        cagr = extra.get("history_cagr")
        yoy = extra.get("history_yoy") or []
        margins = extra.get("margins") or []
        cagr_txt = f"{cagr:.1%}" if cagr is not None else "n/a"
        yoy_txt = f"{min(yoy):.0%} to {max(yoy):.0%}" if yoy else "n/a"
        # margin trend sentence (the question often asks about margins too)
        mtxt = ""
        gm = [m for m in margins if m.get("gross_margin") is not None]
        if len(gm) >= 2:
            g0, g1 = gm[0]["gross_margin"], gm[-1]["gross_margin"]
            trend = "improving" if g1 > g0 else ("declining" if g1 < g0 else "broadly stable")
            mtxt = (f" Gross margin is {trend} ({g0:.1%} FY{gm[0]['year']} → {g1:.1%} FY{gm[-1]['year']}).")

        test = fc if fc is not None else target
        _VERDICT = {"PASS": "reasonable", "WARNING": "optimistic but within the historical range",
                    "FAIL": "not supported by the historical trend"}
        if test is not None and verdict and verdict != "SKIPPED":
            if fc is not None:
                what = f"the forecast of {_fmt(fc, 'currency')}"
            else:
                what = f"a {extra.get('target_desc') or 'stated'} target (≈{_fmt(test, 'currency')})"
            impl_txt = f" (implies ~{implied:.1%} growth)" if implied is not None else ""
            direct = (f"{company}'s {metric}: {what}{impl_txt} appears "
                      f"{_VERDICT.get(verdict, verdict)} versus a {len(extra.get('history_series', []))}-yr "
                      f"history — CAGR {cagr_txt}, but YoY ranged {yoy_txt}, so the trend is "
                      f"{'volatile' if yoy and (max(yoy) - min(yoy)) > 0.3 else 'steady'}.{mtxt}")
            # cite the per-rule verdicts to the workbook history (trusted baseline)
            act_ref = next((_cite_of(e) for e in evidence
                            if e.value is not None and e.kind in ("statement", "detail")), None)
            rules = [r for r in val.get("rules", []) if r.get("outcome") != "SKIPPED"]
            if rules and act_ref:
                findings = [f"{r['id']}: {r['outcome']} — {r['reason']} [{act_ref}]" for r in rules]
            else:
                findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
            if verdict in ("WARNING", "FAIL"):
                analysis = (f"Validation {verdict} against {val.get('history_points')}y of actuals: "
                            + "; ".join(f"{r['id']} {r['outcome']}" for r in rules) + ".")
        else:
            # nothing concrete to validate (no external forecast and no stated target)
            hs = extra.get("history_series", [])
            span = extra.get("history_span")
            if hs and span:
                direct = (f"No {metric} forecast on record for {company} and no target was "
                          f"stated. History: {_fmt(hs[0]['value'], 'currency')} FY{span[0]} → "
                          f"{_fmt(hs[-1]['value'], 'currency')} FY{span[1]} (CAGR {cagr_txt}); "
                          f"YoY ranged {yoy_txt}.{mtxt} State a target (e.g. a growth %) to validate it.")
                findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
            else:
                direct = f"No {metric} history available to validate a forecast for {company}."

    elif frame.intent == "trend_analysis":
        metric = (extra.get("trend_metric") or "metric").replace("_", " ")
        series = extra.get("series", [])
        if not series:
            direct = f"No {metric} series available for {company}."
        else:
            span = extra.get("span")
            cagr = extra.get("cagr")
            first, last = series[0]["value"], series[-1]["value"]
            if extra.get("aggregation"):
                # answer the aggregation directly; report both senses of "increase"
                avg_abs = extra.get("avg_abs_increase")
                avg_pct = extra.get("avg_growth_pct")
                parts = []
                if avg_pct is not None:
                    parts.append(f"average annual growth {avg_pct:.1%}")
                if avg_abs is not None:
                    parts.append(f"average absolute increase {_fmt(avg_abs, 'currency')}/yr")
                if cagr is not None:
                    parts.append(f"CAGR {cagr:.1%}")
                direct = (f"{company}'s {metric} over FY{span[0]}-FY{span[1]}: "
                          + "; ".join(parts) + ".")
            else:
                direction = "rose" if last > first else ("fell" if last < first else "was flat")
                cagr_txt = f" (CAGR {cagr:.1%})" if cagr is not None else ""
                direct = (f"{company}'s {metric} {direction} from {_fmt(first, 'currency')} "
                          f"(FY{span[0]}) to {_fmt(last, 'currency')} (FY{span[1]}){cagr_txt}.")
            findings = [f"FY{p['year']}: {_fmt(p['value'], 'currency')} [{_cite_of(e)}]"
                        for p, e in zip(series, evidence)]
            flagged = extra.get("flagged_years") or {}
            if flagged:
                yrs = ", ".join(f"FY{y} ({s})" for y, s in sorted(flagged.items()))
                analysis = (f"Data caveat: {yrs} flagged in the workbook's validation "
                            f"ledger - included but may distort the average.")

    elif frame.intent == "dividend_analysis":
        payouts = extra.get("payouts", [])
        if payouts:
            latest = payouts[0]
            direct = (f"{company}'s most recent payout: {latest['claim']} "
                      f"({latest.get('date')}). {len(payouts)} payout(s) on record.")
            findings = [f"{p['claim']} ({p.get('date')}) [{_cite_of(e)}]"
                        for p, e in zip(payouts, evidence)]
        else:
            direct = (f"No payout history available for {company}: "
                      f"{extra.get('note', 'unavailable')}.")

    elif frame.intent in ("news_impact", "earnings_review"):
        if evidence:
            direct = f"{len(evidence)} recent item(s) for {company}."
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence_rank.top(evidence, 8)]
        else:
            direct = f"No recent external items available for {company}."

    elif frame.intent == "edit_history":
        # The structured payload (built by the handler) drives the UI's rich rendering (timestamp
        # chips + arrows). `direct_answer` here is a plain-text fallback (previews/accessibility).
        eh = (extra or {}).get("edit_history") or {}
        edit_history_payload = eh or None
        direct = eh.get("lead") or "No change history available."
        if eh.get("mode") == "list" and eh.get("items"):
            parts = []
            for it in eh["items"]:
                cell = f"/{it['cell']}" if it.get("cell") else ""
                if it.get("kind") == "verify":
                    vref = ((it.get("verified_sheet") + "/") if it.get("verified_sheet") else "") \
                        + (it.get("verified_cell") or "")
                    rhs = "manually verified" + (f" ({vref})" if vref else "")
                    parts.append(f"{it['timestamp']} {it['sheet']}{cell}: {rhs}")
                else:
                    parts.append(f"{it['timestamp']} {it['sheet']}{cell}: "
                                 f"{it.get('old') or '(blank)'} → {it.get('new') or '(blank)'}")
            direct = (direct + " " + " ; ".join(parts)).strip()
        elif eh.get("mode") == "aggregate" and eh.get("by_sheet"):
            direct = direct + " " + ", ".join(f"{s}: {n}" for s, n in eh["by_sheet"].items())
        findings = []   # listing lives in the structured payload; no per-fact citations to enforce

    elif frame.intent == "data_availability":
        av = (extra or {}).get("availability") or {}
        yrs = av.get("years") or []
        yspan = f"{yrs[0]}–{yrs[-1]}" if len(yrs) > 1 else (str(yrs[0]) if yrs else "no")
        names = {"pl": "an income statement (P&L)", "bs": "a balance sheet",
                 "cf": "a cash-flow statement", "equity": "a statement of changes in equity"}
        _stmts = [names.get(s, s) for s in av.get("statements", [])]
        have = ", ".join(_stmts)
        have_and = (" and ".join(_stmts) if len(_stmts) <= 2
                    else ", ".join(_stmts[:-1]) + " and " + _stmts[-1])
        kind = av.get("kind")
        sheet_names = av.get("sheet_names") or []
        if kind == "sheet_index":
            if not sheet_names:
                direct = "I don't have this workbook's sheet ordering, so I can't give a sheet index."
            elif av.get("sheet") is not None:
                i = av["index"]
                direct = (f"“{av['sheet']}” is sheet {i + 1} of {av['total']} "
                          f"(zero-based index {i}).")
            else:
                shown = ", ".join(sheet_names[:15]) + (" …" if len(sheet_names) > 15 else "")
                direct = (f"I couldn't find a sheet matching that name. This workbook's "
                          f"{len(sheet_names)} sheets are: {shown}.")
        elif kind == "sheet_count":
            direct = (f"This workbook has {len(sheet_names)} sheets."
                      if sheet_names else "I don't have this workbook's sheet list.")
        elif kind == "sheets_list":
            if sheet_names:
                direct = (f"This workbook has {len(sheet_names)} sheets: "
                          + ", ".join(sheet_names) + ".")
            else:
                direct = "I don't have this workbook's sheet list."
        elif kind == "company":
            direct = (f"This workbook contains the financial statements of {av['company']}."
                      if av.get("company")
                      else "The company name isn't recorded in this workbook's metadata.")
        elif kind == "category":
            if av.get("present"):
                direct = f"Yes — this workbook includes {av['label']}."
            else:
                direct = (f"No — this workbook does not include {av['label']}. It has "
                          f"{have or 'no recognised statements'}"
                          + (f" covering {yspan}." if yrs else "."))
                if av.get("related"):
                    direct += f" It does carry related items: {', '.join(av['related'])}."
        elif kind == "statements":
            direct = (f"This workbook contains {have_and}." if _stmts
                      else "This workbook has no recognised financial statements.")
        elif kind == "metric_count":
            direct = f"This workbook has {av.get('metric_count', 0)} metric(s) available."
        elif kind == "metric":
            direct = (f"Yes — “{av['label']}” is in this workbook." if av.get("present")
                      else f"No — “{av['label']}” is not in this workbook.")
        elif kind == "year":
            yr = av.get("year")
            if av.get("present"):
                direct = f"Yes — {yr} is included; this workbook covers {yspan}."
            else:
                direct = (f"No — {yr} is not included. This workbook covers {yspan}."
                          if yrs else f"No — {yr} is not included; no year data is present.")
        elif kind == "years":
            direct = (f"This workbook covers {yspan}." if yrs
                      else "No year data is present in this workbook.")
        else:  # overview
            direct = (f"This workbook contains {have or 'no recognised statements'}"
                      + (f" covering {yspan}" if yrs else "")
                      + f", with {av.get('metric_count', 0)} metric(s).")
        findings = []

    else:
        direct = f"Could not handle query: intent '{frame.intent}' not supported yet."

    # investor audience: keep it concise — drop the formula mechanics
    if audience == "investor" and frame.intent == "ratio_analysis" and primary and primary.value is not None:
        analysis = ""

    # LLM prose (3.3): use ONLY if it passes the numeric guard; else keep deterministic.
    prose_source = "deterministic"
    if llm_analysis:
        if safety.verify_prose(llm_analysis, frame, evidence, calcs, citations):
            llm_text = llm_analysis.strip()
            # For intents where the deterministic `direct` is a context summary / placeholder
            # (forecast_validation with no external record, risk_assessment, news_impact),
            # the LLM IS the answer — promote it to direct_answer and demote the
            # deterministic text to supporting_analysis so users see the real answer first.
            _promote_intents = {"forecast_validation", "risk_assessment", "news_impact",
                                "earnings_review", "agent", "validation"}
            if frame.intent in _promote_intents:
                analysis = direct   # deterministic context summary moves to supporting
                direct = llm_text   # LLM assessment becomes the primary answer
            else:
                analysis = llm_text
            prose_source = "llm"
        else:
            # Fell back to the deterministic answer because the prose carried a number not
            # backed by evidence/calcs. For an AGENT query the prose IS the agent's answer, so
            # this is why a verified-looking answer can degrade to the "Compiled N data points"
            # summary — log it (with a snippet) so the offending figure is debuggable.
            _log.info("numeric guard rejected %s prose for intent=%s — using deterministic "
                      "fallback; prose=%r", "agent" if frame.intent == "agent" else "llm",
                      frame.intent, (llm_analysis or "")[:160], extra={"component": "Respond"})

    # surface numeric divergences explicitly (both sides + authority/chronology verdict),
    # appended so they are never silently dropped by the LLM/deterministic prose choice.
    numeric_div = [c for c in conflicts if c.type in ("internal_vs_external", "cross_api")]
    if numeric_div:
        dtext = divergence.present(numeric_div)
        analysis = f"{analysis} {dtext}".strip() if analysis else dtext

    # --- claim-level citation enforcement (L8a) ---------------------------
    # drop any finding whose backing citation lacks resolvable provenance; if a
    # findings-bearing answer loses ALL of them (and no computed value remains),
    # degrade to a clean insufficient-evidence response rather than ship uncited.
    valid_refs = citation_enforce.valid_ref_ids(citations)
    n_findings_before = len(findings)
    findings, dropped_claims = citation_enforce.enforce_findings(findings, valid_refs)
    if dropped_claims:   # decision log: why claims didn't ship (uncitable provenance)
        _log.info("dropped %d uncitable claim(s) for intent=%s: %s",
                  len(dropped_claims), frame.intent, dropped_claims[:3],
                  extra={"component": "Respond"})
    insufficient = (n_findings_before > 0 and not findings
                    and not (primary and primary.value is not None))
    if insufficient:
        _log.info("INSUFFICIENT_EVIDENCE: all %d candidate claim(s) lacked resolvable "
                  "citations (intent=%s, company=%s)", n_findings_before, frame.intent,
                  company, extra={"component": "Respond"})
        direct = (f"Insufficient citable evidence to answer this query for {company}"
                  + (f" (FY{frame.year})" if frame.year else "") + ".")
        analysis = ""
        prose_source = "deterministic"

    used = sorted({f.sheet for ev in evidence for f in ev.fact_refs})
    evidence_used = [f"Workbook sheet: {s}" for s in used]
    if any(e.kind == "insight" for e in evidence):
        evidence_used.append("Insights repository")
    ext = sorted({(e.citations[0].locator.get("source") if e.citations else "external")
                  for e in evidence if e.kind == "external"})
    evidence_used += [f"External: {s}" for s in ext if s]

    withheld_labels = [f"{w.metric or w.label} {w.year}" for w in withheld]

    cov = dict(coverage or {})
    if dropped_claims:
        cov["dropped_claims"] = len(dropped_claims)
    qc = extra.get("qual_coverage")
    if qc:
        cov["qualitative"] = {"run_status": qc.get("run_status"),
                              "admitted_categories": qc.get("admitted_categories"),
                              "unmapped_count": qc.get("unmapped_count")}
    conf_out = confidence
    if insufficient:
        cov["insufficient_evidence"] = True
        conf_out = ConfidenceReport(
            band="Low", score=0.0,
            reasons=["all candidate claims lacked resolvable citations"])

    return Response(
        direct_answer=direct,
        key_findings=findings,
        supporting_analysis=analysis,
        calculations=calcs,
        evidence_used=evidence_used,
        citations=citations,
        conflicts=conflicts,
        withheld=withheld_labels,
        confidence=conf_out,
        prose_source=prose_source,
        coverage=cov,
        edit_history=edit_history_payload,
    )
