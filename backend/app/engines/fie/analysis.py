"""Parity primitives — reuse the engine's DETERMINISTIC tools/handlers to cover the categories the
workbook/formula/compute primitives don't: data-validation (footing/identity/anomaly), qualitative
insights, forward projection, and the edit-history log.

Each wraps a deterministic tool/handler by handing it a lightweight shim context, then harvests the
cited evidence/calcs it populated. No new financial logic — the numbers still come from the same
audited deterministic code, so citations + the numeric guard hold.
"""

from __future__ import annotations

from types import SimpleNamespace

from . import agent
from .models import CalcResult


def _ctx(*, pending_edits=None, now=None):
    return SimpleNamespace(
        trace_id=None, evidence=[], calcs=[], conflicts=[], selected_insights=[],
        insight_resolutions=[], llm_analysis=None, degraded=False, partial_coverage=False,
        extra=None, total_insights=0, superseded=0, dumper=None,
        now=now, pending_edits=pending_edits or [],
    )


def validate(engine, frame):
    """Deterministic statement audit: accounting identities + component footing + anomalies
    (the same check_balance/scan_anomalies the audit tools use, with the subtotal/sign fixes)."""
    ctx = _ctx()
    report = agent._t_check_balance(engine, frame, ctx, {})
    anom = agent._t_scan_anomalies(engine, frame, ctx, {})
    checks_run = report.get("checks_run") or 0
    breaks = report.get("breaks") or []
    anomalies = anom.get("anomalies") or []
    all_ok = report.get("all_ok")
    # register the audit COUNTS as calcs so the numeric guard admits "40 checks / 4 did not
    # reconcile" in the composed prose (the same trick the edit-history primitive uses).
    calcs = list(ctx.calcs) + [
        CalcResult(formula_id="audit_count", value=float(checks_run), confidence="High"),
        CalcResult(formula_id="audit_count", value=float(len(breaks)), confidence="High"),
        CalcResult(formula_id="audit_count", value=float(len(anomalies)), confidence="High"),
    ]
    if all_ok:
        lead = (f"All {checks_run} consistency checks reconciled across the statements — "
                "the balance sheet balances and components foot to their totals.")
    else:
        yrs = sorted({b.get("year") for b in breaks if b.get("year")})
        where = f" (e.g. {', '.join(str(y) for y in yrs[:4])})" if yrs else ""
        lead = (f"Of {checks_run} consistency checks, {len(breaks)} did not reconcile{where}; "
                f"{len(anomalies)} anomaly(ies) flagged.")
    res = {
        "kind": "validation",
        "lead": lead,
        "checks_run": checks_run,
        "all_ok": all_ok,
        "breaks": breaks,
        "anomalies": anomalies,
    }
    return res, ctx.evidence, calcs


def insights(engine, frame):
    """Qualitative management-commentary themes (selected + deconflicted), as cited insight
    evidence — so qualitative answers are SOURCED, not invented from the numbers."""
    ctx = _ctx()
    out = agent._t_insights(engine, frame, ctx, {})
    return {"kind": "insights", "insights": out.get("insights") or []}, ctx.evidence, ctx.calcs


def project(engine, frame, args=None):
    """Forward projection as a SCENARIO (low/base/high), never a forecast of fact."""
    ctx = _ctx()
    out = agent._t_project(engine, frame, ctx, args or {})
    return {"kind": "forecast", **out}, ctx.evidence, ctx.calcs


def edit_history(engine, frame, *, pending_edits=None, now=None):
    """The user's own edit log (saved History sheet + unsaved pending edits). Returns the
    structured payload the UI renders; no financial evidence."""
    ctx = _ctx(pending_edits=pending_edits, now=now)
    engine._h_edit_history(frame, ctx, None)   # populates ctx.extra["edit_history"]
    eh = (ctx.extra or {}).get("edit_history") or {}
    # register the listing counts as calcs so the numeric guard admits "22 changes / showing 20"
    # in the composed summary (the cell refs/timestamps live in the UI payload, not the prose).
    calcs = [CalcResult(formula_id="edit_count", value=float(eh[k]), confidence="High")
             for k in ("total", "shown", "open_count") if isinstance(eh.get(k), (int, float))]
    return {"kind": "edit_history", "lead": eh.get("lead"), "payload": eh}, [], calcs
