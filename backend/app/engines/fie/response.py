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
    primary = calcs[0] if calcs else None

    if frame.intent == "ratio_analysis":
        if primary and primary.value is not None:
            direct = (f"{company}'s {frame.formula.replace('_', ' ')} for {frame.year} "
                      f"is {_fmt(primary.value, primary.unit)}.")
            analysis = (f"Computed as {primary.expression}. Inputs drawn from the "
                        f"workbook (authoritative) and cited below.")
        else:
            note = primary.note if primary else "insufficient data"
            direct = (f"Unable to compute {frame.formula.replace('_', ' ')} for "
                      f"{frame.year}: {note}.")
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
            else:
                sugg = extra.get("suggestions") or []
                hint = (" Available metrics include: "
                        + ", ".join(s.replace('_', ' ') for s in sugg[:8]) + "."
                        ) if sugg else ""
                direct = f"{company}'s {metric} for {frame.year} was not found in the workbook.{hint}"
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in valued]

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
        fc, act = extra.get("forecast"), extra.get("latest_actual")
        metric = (extra.get("metric") or "metric").replace("_", " ")
        if fc is None:
            history_series = extra.get("history_series", [])
            cagr = extra.get("history_cagr")
            span = extra.get("history_span")
            if history_series and span:
                cagr_txt = f" (CAGR {cagr:.1%})" if cagr is not None else ""
                direct = (
                    f"No external {metric} forecast on record for {company}. "
                    f"Historical trend: {metric} {('rose' if (history_series[-1]['value'] or 0) > (history_series[0]['value'] or 0) else 'fell')} "
                    f"from {_fmt(history_series[0]['value'], 'currency')} (FY{span[0]}) to "
                    f"{_fmt(history_series[-1]['value'], 'currency')} (FY{span[1]}){cagr_txt}. "
                    f"Assess the stated target against this {len(history_series)}-year trend."
                )
                findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
            else:
                year_str = f" (FY{extra.get('year')})" if extra.get("year") else ""
                base = f"No {metric} forecast on record for {company}{year_str}, so it cannot be validated against a target."
                if act is not None:
                    direct = (base + f" Latest actual {metric} is {_fmt(act, 'currency')} "
                              f"(Rs '000, FY{extra.get('latest_year')}).")
                    findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
                else:
                    direct = base
        elif act is not None:
            delta = (fc - act) / act if act else None
            direction = "above" if fc > act else "below"
            val = extra.get("validation") or {}
            verdict = val.get("outcome")
            vtxt = f" Validation: {verdict}." if verdict and verdict != "SKIPPED" else ""
            direct = (f"{company}'s {metric} forecast for {extra.get('year')} "
                      f"({_fmt(fc, 'currency')}) is {abs(delta):.1%} {direction} the latest "
                      f"actual ({_fmt(act, 'currency')}, FY{extra.get('latest_year')}).{vtxt}")
            # per-rule verdicts cited to the workbook history (the trusted baseline)
            act_ref = next((_cite_of(e) for e in evidence
                            if e.value is not None and e.kind in ("statement", "detail")), None)
            rules = [r for r in val.get("rules", []) if r.get("outcome") != "SKIPPED"]
            if rules and act_ref:
                findings = [f"{r['id']}: {r['outcome']} — {r['reason']} [{act_ref}]" for r in rules]
            else:
                findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
            if verdict in ("WARNING", "FAIL"):
                analysis = (f"Forecast validation ({verdict}) against {val.get('history_points')}y "
                            f"of actuals: " + "; ".join(f"{r['id']} {r['outcome']}" for r in rules) + ".")
        else:
            direct = (f"{company}'s {metric} forecast for {extra.get('year')} is "
                      f"{_fmt(fc, 'currency')}; no recent actual to compare.")

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
                                "earnings_review"}
            if frame.intent in _promote_intents:
                analysis = direct   # deterministic context summary moves to supporting
                direct = llm_text   # LLM assessment becomes the primary answer
            else:
                analysis = llm_text
            prose_source = "llm"
        # else: silently fall back — the LLM tried to introduce an unbacked number

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
    )
