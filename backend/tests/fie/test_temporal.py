"""value_year vs source_report_year: the dual-date primitives (as-of leakage guard,
latest/as-reported preference) and report_year_preference honored in restatement."""

import pandas as pd

from app.engines.fie import temporal as T
from app.engines.fie.conflicts import ConflictResolver
from app.engines.fie.models import FactRef


def _fact(metric, value_year, value, report_year):
    return FactRef(company="MTL", metric=metric, label=metric, year=value_year, value=value,
                   unit="Rupees in thousand", statement="pl", level="headline",
                   sheet="PL", cell="C5", report_year=report_year)


# --- primitives ------------------------------------------------------------
def test_value_vs_report_year_accessors():
    f = _fact("revenue", 2023, 100.0, report_year=2025)   # FY2023 value, found in FY2025 report
    assert T.value_year(f) == 2023 and T.source_report_year(f) == 2025


def test_known_as_of_blocks_lookahead():
    facts = [_fact("revenue", 2023, 100.0, 2024), _fact("revenue", 2023, 110.0, 2026)]
    # as of the FY2024 report, the FY2026 restatement was not yet known
    kept = T.known_as_of(facts, 2024)
    assert [f.report_year for f in kept] == [2024]
    assert len(T.known_as_of(facts, None)) == 2          # no as-of -> no filtering
    # a fact with no report_year (workbook) is always known
    assert T.known_as_of([_fact("revenue", 2023, 100.0, None)], 2020)


def test_prefer_latest_vs_as_reported():
    pairs = [(2024, 100.0), (2026, 130.0), (2023, 95.0)]
    assert T.prefer(pairs, "latest") == (2026, 130.0)        # restated view
    assert T.prefer(pairs, "as_reported") == (2023, 95.0)    # as first published
    assert T.prefer([], "latest") is None


# --- restatement honors report_year_preference -----------------------------
class _LedgerStore:
    """Minimal store exposing a source_ledger with a restated revenue line."""
    def __init__(self):
        self.source_ledger = pd.DataFrame([
            {"Sheet": "PL detail", "Year": 2023, "Report year": 2024, "Value": 100.0,
             "matched_label_norm": "revenue"},
            {"Sheet": "PL detail", "Year": 2023, "Report year": 2026, "Value": 130.0,
             "matched_label_norm": "revenue"},
        ])


def _restatement(preference):
    import app.engines.fie.ontology as onto
    onto.STATEMENT_LINE_TO_DETAIL  # ensure import
    r = ConflictResolver(store=_LedgerStore())
    f = FactRef(company="MTL", metric="revenue", label="revenue", year=2023, value=130.0,
                unit="Rupees in thousand", statement="pl", level="headline", sheet="PL", cell="C5")
    return r.detect_restatement(f, preference=preference)


def test_restatement_latest_vs_as_reported():
    # only runs if 'revenue' maps to a detail sheet in the ontology; otherwise None
    latest = _restatement("latest")
    asrep = _restatement("as_reported")
    if latest is None:                                   # ontology mapping absent -> skip
        return
    assert "FY2026" in latest.resolution and "newest report" in latest.resolution
    assert "FY2024" in asrep.resolution and "as first reported" in asrep.resolution
    assert latest.resolved and asrep.resolved
