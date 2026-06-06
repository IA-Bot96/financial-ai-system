"""Volatility-adaptive forecast validation (L6).

Ported from the legacy FVE revenue rules: validate a submitted/forecast value against
the company's OWN historical actual series with typed PASS / WARNING / FAIL verdicts,
a reason, and a calc trace. Thresholds are **history-relative and volatility-aware** —
they adapt to each metric's own variability rather than using fixed cut-offs. Metric-
agnostic. The workbook history is the trusted baseline.

Three independent rules, aggregated worst-wins:
  - growth_band       forecast-implied growth vs the [min,max] band of historical YoY
  - trend_break       deviation of implied growth from the median, scaled by σ; plus a
                      direction-reversal check (history up, forecast down — or vice versa)
  - plausibility      forecast vs historical max/latest/average scale multiples
"""

from __future__ import annotations

import statistics
from typing import Optional

PASS, WARNING, FAIL, SKIPPED = "PASS", "WARNING", "FAIL", "SKIPPED"
_SEVERITY = {SKIPPED: 0, PASS: 1, WARNING: 2, FAIL: 3}


def _growths(values: list[float]) -> list[float]:
    """YoY growth rates across the value series (skips zero denominators)."""
    return [(b - a) / a for a, b in zip(values, values[1:]) if a]


def _implied_growth(values: list[float], forecast: float) -> Optional[float]:
    last = values[-1]
    return (forecast - last) / last if last else None


def growth_band_rule(values: list[float], forecast: float) -> dict:
    """PASS inside the historical YoY [min,max] band; WARNING within a 10pp margin of
    the nearest boundary; FAIL beyond. Needs >= 2 years."""
    if len(values) < 2:
        return {"id": "growth_band", "outcome": SKIPPED, "reason": "needs >= 2 years of history"}
    g = _growths(values)
    implied = _implied_growth(values, forecast)
    if not g or implied is None:
        return {"id": "growth_band", "outcome": SKIPPED, "reason": "no usable growth history"}
    lo, hi = min(g), max(g)
    if lo - 1e-9 <= implied <= hi + 1e-9:
        outcome, reason = PASS, "implied growth within historical range"
    else:
        boundary = lo if implied < lo else hi
        margin = 0.10                              # 10 percentage points
        if abs(implied - boundary) <= margin:
            outcome, reason = WARNING, "implied growth just outside historical range"
        else:
            outcome, reason = FAIL, "implied growth far outside historical range"
    return {"id": "growth_band", "outcome": outcome, "reason": reason,
            "detail": {"implied_growth": round(implied, 4),
                       "hist_min": round(lo, 4), "hist_max": round(hi, 4)}}


def trend_break_rule(values: list[float], forecast: float) -> dict:
    """Deviation of implied growth from the median historical growth, with thresholds
    scaled by volatility (σ): PASS <= max(0.05,σ), FAIL > max(0.20,4σ), WARNING between.
    A direction reversal (consistent-sign history, opposite-sign forecast) is its own
    break — a strong reversal (>=10%) always FAILs. Needs >= 3 years."""
    if len(values) < 3:
        return {"id": "trend_break", "outcome": SKIPPED, "reason": "needs >= 3 years of history"}
    g = _growths(values)
    implied = _implied_growth(values, forecast)
    if len(g) < 2 or implied is None:
        return {"id": "trend_break", "outcome": SKIPPED, "reason": "no usable growth history"}

    # direction reversal: all-positive (or all-negative) history, opposite-sign forecast
    if all(x > 0 for x in g) and implied < 0 or all(x < 0 for x in g) and implied > 0:
        if abs(implied) >= 0.10:
            return {"id": "trend_break", "outcome": FAIL,
                    "reason": "strong direction reversal vs a consistent historical trend",
                    "detail": {"implied_growth": round(implied, 4)}}
        return {"id": "trend_break", "outcome": WARNING,
                "reason": "direction reversal vs historical trend",
                "detail": {"implied_growth": round(implied, 4)}}

    median = statistics.median(g)
    sigma = statistics.pstdev(g)
    consistent = max(0.05, sigma)
    fail_thr = max(0.20, 4 * sigma)
    dev = abs(implied - median)
    if dev <= consistent:
        outcome, reason = PASS, "implied growth consistent with the historical trend"
    elif dev <= fail_thr:
        outcome, reason = WARNING, "implied growth deviates from the historical trend"
    else:
        outcome, reason = FAIL, "implied growth breaks the historical trend"
    return {"id": "trend_break", "outcome": outcome, "reason": reason,
            "detail": {"implied_growth": round(implied, 4), "median_growth": round(median, 4),
                       "volatility": round(sigma, 4), "deviation": round(dev, 4)}}


def plausibility_rule(values: list[float], forecast: float) -> dict:
    """Scale plausibility vs historical max / latest / average (worst-wins). Needs >= 2
    years. Flags a forecast that is implausibly large or small relative to history."""
    if len(values) < 2:
        return {"id": "plausibility", "outcome": SKIPPED, "reason": "needs >= 2 years of history"}
    hmax, hmin = max(values), min(values)
    latest, avg = values[-1], statistics.fmean(values)
    checks: list[tuple[str, float]] = []  # (outcome, severity proxy via name)
    notes: list[str] = []

    if hmax:
        mult = forecast / hmax
        if mult >= 2.0 or mult <= 0.25:
            checks.append((FAIL, "implausible vs historical max"))
        elif mult >= 1.5 or mult <= 0.5:
            checks.append((WARNING, "stretched vs historical max"))
    if latest:
        d = abs(forecast - latest) / latest
        if d >= 1.0:
            checks.append((FAIL, "far from the latest actual"))
        elif d >= 0.5:
            checks.append((WARNING, "well above/below the latest actual"))
    if avg:
        d = abs(forecast - avg) / avg
        if d >= 1.5:
            checks.append((FAIL, "far from the historical average"))
        elif d >= 0.75:
            checks.append((WARNING, "well above/below the historical average"))

    scale_position = ("above historical max" if forecast > hmax
                      else "below historical min" if forecast < hmin else "within range")
    if not checks:
        return {"id": "plausibility", "outcome": PASS,
                "reason": f"forecast scale is plausible ({scale_position})",
                "detail": {"scale_position": scale_position}}
    worst = max(checks, key=lambda c: _SEVERITY[c[0]])
    return {"id": "plausibility", "outcome": worst[0], "reason": worst[1],
            "detail": {"scale_position": scale_position,
                       "vs_max": round(forecast / hmax, 3) if hmax else None}}


def validate_forecast(history: list[tuple[int, float]], forecast: Optional[float]) -> dict:
    """Run all applicable rules over the (year, value) history and aggregate worst-wins.

    Returns {outcome, history_points, implied_growth, rules:[...]}. ``history`` is the
    company's own historical actual series (the trusted baseline); ``forecast`` is the
    value under test (e.g. a submitted target)."""
    series = sorted((y, v) for y, v in history if v is not None)
    values = [v for _, v in series]
    if forecast is None or len(values) < 2:
        return {"outcome": SKIPPED, "history_points": len(values),
                "implied_growth": None, "rules": [],
                "reason": "insufficient history or no forecast to validate"}

    rules = [growth_band_rule(values, forecast),
             trend_break_rule(values, forecast),
             plausibility_rule(values, forecast)]
    scored = [r for r in rules if r["outcome"] != SKIPPED]
    outcome = max((r["outcome"] for r in scored), key=lambda o: _SEVERITY[o]) if scored else SKIPPED
    return {"outcome": outcome, "history_points": len(values),
            "implied_growth": _implied_growth(values, forecast), "rules": rules}
