"""Scale normalization: canonical-PKR conversion, cross-unit ratios, and 10^k
scale-disagreement detection (the workbook-thousand vs report-million vs absolute-PKR
problem). Pure unit tests — no engine, no network."""

import pytest

from app.engines.fie import scale


# --- unit_scale: which units are monetary magnitudes -----------------------
@pytest.mark.parametrize("unit,mult", [
    ("Rupees in thousand", 1_000.0),
    ("Rs '000", 1_000.0),
    ("Rs. million", 1_000_000.0),
    ("rupees in million", 1_000_000.0),
    ("Rs. billion", 1_000_000_000.0),
    ("PKR", 1.0),
    ("absolute", 1.0),
])
def test_unit_scale_magnitudes(unit, mult):
    assert scale.unit_scale(unit) == mult
    assert scale.is_magnitude(unit)


@pytest.mark.parametrize("unit", [
    "PKR/share", "per share", "x", "ratio", "percent", "%", "shares",
    "PKR/USD", None, "", "index points",
])
def test_unit_scale_non_magnitudes(unit):
    assert scale.unit_scale(unit) is None
    assert not scale.is_magnitude(unit)


# --- to_canonical_pkr ------------------------------------------------------
def test_to_canonical_pkr():
    assert scale.to_canonical_pkr(8076.3, "Rupees in thousand") == 8_076_300.0
    assert scale.to_canonical_pkr(52108.997, "Rs. million") == 52_108_997_000.0
    assert scale.to_canonical_pkr(112_453_173_207.61, "PKR") == 112_453_173_207.61
    assert scale.to_canonical_pkr(None, "Rupees in thousand") is None
    assert scale.to_canonical_pkr(563.63, "PKR/share") is None       # per-share not scaled


# --- magnitude_ratio: the P/B fix -----------------------------------------
def test_magnitude_ratio_same_unit():
    # both in thousands -> dimensionless, unaffected by the shared scale
    assert scale.magnitude_ratio(112_453_173.21, "Rupees in thousand",
                                 8_076_300.0, "Rupees in thousand") == pytest.approx(13.924, rel=1e-3)


def test_magnitude_ratio_absolute_over_thousand():
    # market cap in ABSOLUTE PKR over equity in THOUSANDS -> still correct (~13.9x),
    # NOT 1000x off. This is the bug the normalization prevents.
    pb = scale.magnitude_ratio(112_453_173_207.61, "PKR",
                               8_076_300.0, "Rupees in thousand")
    assert pb == pytest.approx(13.924, rel=1e-3)


def test_magnitude_ratio_guards():
    assert scale.magnitude_ratio(100, "x", 5, "Rupees in thousand") is None   # non-magnitude
    assert scale.magnitude_ratio(100, "PKR", 0, "PKR") is None                # zero denom
    assert scale.magnitude_ratio(None, "PKR", 5, "PKR") is None


# --- detect_scale_factor ---------------------------------------------------
def test_detect_scale_factor():
    assert scale.detect_scale_factor(112_453_173_207.0, 112_453_173.21) == 3   # ×1000 mislabel
    assert scale.detect_scale_factor(52_108_997_000.0, 52_108.997) == 6        # thousand vs million
    assert scale.detect_scale_factor(112_453_173.21, 112_453_173_207.0) == -3  # inverse
    assert scale.detect_scale_factor(100.0, 100.0) is None                     # equal, no factor
    assert scale.detect_scale_factor(100.0, 137.0) is None                     # genuine difference
    assert scale.detect_scale_factor(0, 5) is None


# --- reconcile: verdicts for the future conflict layer ---------------------
def test_reconcile_agree():
    r = scale.reconcile(8076.3, "Rupees in thousand", 8.0763, "Rs. million")
    assert r["verdict"] == "agree"                       # 8.0763M == 8076.3 thousand


def test_reconcile_scale_disagreement():
    # same declared unit but values differ by exactly 1000x -> likely a mislabel
    r = scale.reconcile(8_076_300.0, "Rupees in thousand", 8076.3, "Rupees in thousand")
    assert r["verdict"] == "scale_disagreement" and r["factor_k"] == 3


def test_reconcile_divergent():
    r = scale.reconcile(100.0, "PKR", 137.0, "PKR")
    assert r["verdict"] == "divergent" and r["factor_k"] is None


def test_reconcile_not_comparable():
    r = scale.reconcile(15.5, "x", 8076.3, "Rupees in thousand")
    assert r["verdict"] == "not_comparable"
