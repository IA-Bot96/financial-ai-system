"""Scale normalization (L6 support).

Convert monetary MAGNITUDES to a single canonical base — **absolute PKR (rupees)** —
*before* any cross-source comparison, and detect pure power-of-1000 scale
disagreements. This is the centralized fix for the recurring mismatch between:

  - the workbook            → "Rupees in thousand"   (×1e3)
  - analysis_reports        → "Rs. million"           (×1e6)
  - market data / screener  → absolute PKR            (×1)

Ported in spirit from the legacy MSIL scale governance (financial_year_consolidator
`_has_scale_disagreement`): a ~5% ratio tolerance around 10^k factors.

Only monetary magnitudes are scaled. Per-share values (PKR/share), ratios (x),
percentages, share counts and unknown/display units are NOT magnitudes — they return
None and callers must never scale them.
"""

from __future__ import annotations

from typing import Optional

_THOUSAND = 1_000.0
_MILLION = 1_000_000.0
_BILLION = 1_000_000_000.0

# ratio tolerance for treating a residual as a pure 10^k scale factor
DEFAULT_REL_TOL = 0.05
_SCALE_POWERS = (3, 6, 9, -3, -6, -9)


def unit_scale(unit: Optional[str]) -> Optional[float]:
    """Multiplier from `unit` to canonical absolute PKR, or None if `unit` is not a
    monetary magnitude (per-share / ratio / percent / count / fx / unknown)."""
    if not unit:
        return None
    u = unit.strip().lower()
    # non-magnitude units — explicitly not scalable
    if any(tok in u for tok in ("/share", "per share", "ratio", "percent", "%",
                                "share", "/usd", "usd", "bps", "index")):
        return None
    if u in ("x", "ratio") or u.endswith(" x"):
        return None
    # magnitude scales (order matters: check scale words before bare-currency)
    if "thousand" in u or "'000" in u or "000s" in u:
        return _THOUSAND
    if "million" in u or " mn" in u or u.endswith(" mn"):
        return _MILLION
    if "billion" in u or " bn" in u or u.endswith(" bn"):
        return _BILLION
    if "pkr" in u or "rupee" in u or u in ("rs", "rs.", "absolute", "currency"):
        return 1.0
    return None


def is_magnitude(unit: Optional[str]) -> bool:
    """True when `unit` is a monetary magnitude that can be scale-normalized."""
    return unit_scale(unit) is not None


def to_canonical_pkr(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """`value` (in `unit`) expressed in canonical absolute PKR; None if value is None
    or `unit` is not a monetary magnitude."""
    if value is None:
        return None
    s = unit_scale(unit)
    if s is None:
        return None
    return float(value) * s


def magnitude_ratio(num_value: Optional[float], num_unit: Optional[str],
                    den_value: Optional[float], den_unit: Optional[str]
                    ) -> Optional[float]:
    """Dimensionless ratio num/den after normalizing BOTH to canonical PKR — so a
    market-cap in absolute PKR over a workbook equity in thousands is correct
    regardless of either side's declared scale. None if either side isn't a monetary
    magnitude or the denominator is 0/None."""
    n = to_canonical_pkr(num_value, num_unit)
    d = to_canonical_pkr(den_value, den_unit)
    if n is None or d is None or d == 0:
        return None
    return n / d


def detect_scale_factor(a: Optional[float], b: Optional[float], *,
                        rel_tol: float = DEFAULT_REL_TOL) -> Optional[int]:
    """Return k (so a ≈ b·10^k, k in ±{3,6,9}) if a and b differ by a pure
    power-of-1000 factor within `rel_tol`, else None. Used to flag a likely
    thousand/million/billion mislabel between two values for the SAME fact."""
    if not a or not b:
        return None
    a, b = abs(float(a)), abs(float(b))
    if a == 0 or b == 0:
        return None
    ratio = a / b
    for k in _SCALE_POWERS:
        f = 10.0 ** k
        if abs(ratio - f) <= rel_tol * f:
            return k
    return None


def reconcile(value_a: Optional[float], unit_a: Optional[str],
              value_b: Optional[float], unit_b: Optional[str], *,
              rel_tol: float = DEFAULT_REL_TOL) -> dict:
    """Normalize two monetary values to canonical PKR and classify their relationship
    (for the conflict layer):

      - ``agree``              within ``rel_tol`` after normalization
      - ``scale_disagreement`` differ by a pure 10^k factor (likely unit mislabel) —
                               ``factor_k`` is the power (a ≈ b·10^k)
      - ``divergent``          genuinely different values
      - ``not_comparable``     one/both sides are not a monetary magnitude

    Returns {verdict, canonical_a, canonical_b, factor_k}.
    """
    ca = to_canonical_pkr(value_a, unit_a)
    cb = to_canonical_pkr(value_b, unit_b)
    if ca is None or cb is None:
        return {"verdict": "not_comparable", "canonical_a": ca,
                "canonical_b": cb, "factor_k": None}
    base = max(abs(ca), abs(cb), 1.0)
    if abs(ca - cb) <= rel_tol * base:
        return {"verdict": "agree", "canonical_a": ca, "canonical_b": cb, "factor_k": None}
    k = detect_scale_factor(ca, cb, rel_tol=rel_tol)
    if k is not None:
        return {"verdict": "scale_disagreement", "canonical_a": ca,
                "canonical_b": cb, "factor_k": k}
    return {"verdict": "divergent", "canonical_a": ca, "canonical_b": cb, "factor_k": None}
