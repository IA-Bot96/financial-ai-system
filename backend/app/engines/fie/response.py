"""Response generation (L8b) — Phase 2 deterministic renderer.

Assembles the 7-section structure from structured inputs. No LLM prose yet
(Phase 3 adds it, constrained to these same facts). Every figure shown traces
to a Citation or is omitted/withheld.

See docs/fie_implementation_plan.md §Phase 1 (1.7) / §Phase 2.
"""

from __future__ import annotations

from . import safety
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
        valued = [e for e in evidence if e.value is not None]
        metric = frame.metrics[0].replace("_", " ") if frame.metrics else "metric"
        if valued:
            e = valued[0]
            direct = f"{company}'s {metric} for {frame.year} was {_fmt(e.value, 'currency')} (Rs '000)."
        else:
            direct = f"{company}'s {metric} for {frame.year} was not found in the workbook."
        findings = [f"{e.claim} [{_cite_of(e)}]" for e in valued]

    elif frame.intent == "risk_assessment":
        direct = (f"Identified {len(evidence)} risk-related insight(s) for {company}"
                  + (f" ({frame.year})." if frame.year else "."))
        findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence[:8]]
        if conflicts:
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
        if parts:
            direct = f"{company}'s valuation ({extra.get('ticker')}): " + "; ".join(parts) + "."
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence]
        else:
            direct = (f"Unable to value {company}: {extra.get('note', 'market data unavailable')}. "
                      f"Internal financials remain available.")

    elif frame.intent == "forecast_validation":
        fc, act = extra.get("forecast"), extra.get("latest_actual")
        metric = (extra.get("metric") or "metric").replace("_", " ")
        if fc is None:
            base = (f"No {metric} forecast on record for {company} {extra.get('year')}, "
                    f"so it cannot be validated against a target.")
            if act is not None:
                direct = (base + f" Latest actual {metric} is {_fmt(act, 'currency')} "
                          f"(Rs '000, FY{extra.get('latest_year')}).")
                findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
            else:
                direct = base
        elif act is not None:
            delta = (fc - act) / act if act else None
            direction = "above" if fc > act else "below"
            direct = (f"{company}'s {metric} forecast for {extra.get('year')} "
                      f"({_fmt(fc, 'currency')}) is {abs(delta):.1%} {direction} the latest "
                      f"actual ({_fmt(act, 'currency')}, FY{extra.get('latest_year')}).")
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence if e.value is not None]
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
            findings = [f"{e.claim} [{_cite_of(e)}]" for e in evidence[:8]]
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
            analysis = llm_analysis.strip()
            prose_source = "llm"
        # else: silently fall back — the LLM tried to introduce an unbacked number

    used = sorted({f.sheet for ev in evidence for f in ev.fact_refs})
    evidence_used = [f"Workbook sheet: {s}" for s in used]
    if any(e.kind == "insight" for e in evidence):
        evidence_used.append("Insights repository")
    ext = sorted({(e.citations[0].locator.get("source") if e.citations else "external")
                  for e in evidence if e.kind == "external"})
    evidence_used += [f"External: {s}" for s in ext if s]

    withheld_labels = [f"{w.metric or w.label} {w.year}" for w in withheld]

    return Response(
        direct_answer=direct,
        key_findings=findings,
        supporting_analysis=analysis,
        calculations=calcs,
        evidence_used=evidence_used,
        citations=citations,
        conflicts=conflicts,
        withheld=withheld_labels,
        confidence=confidence,
        prose_source=prose_source,
        coverage=coverage or {},
    )
