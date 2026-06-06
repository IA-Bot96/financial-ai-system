"""Fixed-query regression corpus driver: routing is asserted with no data; the
cross-cutting answer invariants are asserted against the live engine (workbook-gated)."""

import os

import pytest

from app.engines.fie import regression_corpus as RC
from app.engines.fie import FinancialIntelligenceEngine, FinancialFactStore


# --- routing (no workbook needed) ------------------------------------------
@pytest.mark.parametrize("case", RC.CASES, ids=[c.intent for c in RC.CASES])
def test_corpus_query_routes_to_expected_intent(case):
    assert RC.check_routing(case) == []


# --- engine invariants (workbook-gated) ------------------------------------
_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


@_real
def test_full_corpus_against_live_engine():
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    report = RC.run(eng)
    assert report["failed"] == 0, "corpus regressions:\n" + "\n".join(report["issues"])


@_real
def test_corpus_invariants_helper_flags_uncited_finding():
    # the invariant checker actually rejects a fabricated uncited finding
    from app.engines.fie.models import Response
    bad = Response.model_construct(direct_answer="x", key_findings=["no citation here"],
                                   citations=[])
    issues = RC.check_invariants(bad)
    assert any("uncited" in m for m in issues)
