"""Phase 2.9 — golden suite via the development-time extraction validator.

This validates the EXTRACTION (is the workbook fit to feed the FIE?), not any
runtime answer. Face truths are an OCR artifact used only here (architecture §0.3).

The audit (ocr_millat_output_audit.md) recorded that the workbook is NOT fully
reconciled, so we assert the validator *behaves correctly*: it reproduces the
metrics that should tie out and flags the ones that don't — rather than asserting
the workbook is clean (it isn't).
"""

import pytest

from app.engines.fie.devtools.validate_extraction import validate_workbook
from tests.fie.fixtures import golden_millat as g


@pytest.fixture(scope="module")
def report(millat_store):
    face = {2025: g.FACE_TRUTH_2025,
            2024: {f"{k}": v for k, v in g.FACE_TRUTH_2024_RESTATED.items()}}
    return validate_workbook(millat_store, face_truth=face, known_bad=g.V5_BAD_VALUES)


def test_validator_reproduces_clean_metrics(report):
    """Metrics that genuinely tie out are reproduced from the workbook."""
    matched = {(c.metric, c.year) for c in report.checks if c.status == "MATCH"}
    # these match audited face truths (verified during build)
    assert ("gross_profit", 2025) in matched
    assert ("operating_profit", 2025) in matched
    assert ("total_assets", 2025) in matched
    assert ("revenue", 2025) in matched
    assert ("revenue", 2024) in matched  # restated value present


def test_validator_flags_known_extraction_errors(report):
    """The workbook is not fully reconciled; the validator must surface mismatches."""
    mism = {(c.metric, c.year) for c in report.mismatches}
    # PAT 2025 is a known extraction discrepancy (4,998,020 vs face 6,372,928)
    assert ("pat", 2025) in mism
    assert not report.passed  # consistent with manifest.fully_reconciled == False


def test_validator_catches_v5_bad_values(report):
    """None of the known-bad v5 values silently slipped into the fixed workbook."""
    assert report.flagged_bad_missed == []
    assert report.flagged_bad_caught  # all bad values caught


def test_manifest_not_reconciled(millat_store):
    assert millat_store.manifest.get("fully_reconciled") is False
