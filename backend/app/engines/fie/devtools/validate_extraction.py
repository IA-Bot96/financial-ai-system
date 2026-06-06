"""Development-time extraction validator (Phase 2.8).

Certifies that a delivered workbook's financial data is correct *before* the FIE
trusts it. This is NOT on the runtime answer path (architecture §0.3): it compares
the workbook's headline figures against audited PDF face truths and the in-workbook
Validation Ledger, and reports MISMATCHes.

Face truths are an OCR/extraction artifact — permitted here (development) and never
used to answer a user query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

_REL_TOL = 0.005  # 0.5%


@dataclass
class MetricCheck:
    metric: str
    year: int
    workbook: Optional[float]
    face_truth: float
    status: str  # MATCH | MISMATCH | MISSING
    diff: Optional[float] = None


@dataclass
class ValidationReport:
    company: str
    checks: list[MetricCheck] = field(default_factory=list)
    flagged_bad_caught: list[str] = field(default_factory=list)
    flagged_bad_missed: list[str] = field(default_factory=list)

    @property
    def mismatches(self) -> list[MetricCheck]:
        return [c for c in self.checks if c.status == "MISMATCH"]

    @property
    def passed(self) -> bool:
        return not self.mismatches and not self.flagged_bad_missed

    def summary(self) -> dict:
        return {
            "company": self.company,
            "checks": len(self.checks),
            "match": sum(c.status == "MATCH" for c in self.checks),
            "mismatch": sum(c.status == "MISMATCH" for c in self.checks),
            "missing": sum(c.status == "MISSING" for c in self.checks),
            "bad_values_caught": len(self.flagged_bad_caught),
            "bad_values_missed": len(self.flagged_bad_missed),
            "passed": self.passed,
        }


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _REL_TOL * max(abs(b), 1.0)


def validate_workbook(store, *, face_truth: dict[int, dict[str, float]],
                      known_bad: list[dict] | None = None) -> ValidationReport:
    """Compare a store's headline metrics against per-year face truths.

    ``face_truth``: {year: {metric: value}}.
    ``known_bad``: optional [{metric, year, bad, truth}] — asserts the validator
    flags the bad value (i.e., workbook does NOT silently equal the bad value).
    """
    report = ValidationReport(company=store.company)

    for year, metrics in face_truth.items():
        for metric, truth in metrics.items():
            try:
                wb_val = store.lookup(metric, year).value
            except KeyError:
                wb_val = None
            if wb_val is None:
                report.checks.append(MetricCheck(metric, year, None, truth, "MISSING"))
            elif _close(wb_val, truth):
                report.checks.append(MetricCheck(metric, year, wb_val, truth, "MATCH", 0.0))
            else:
                report.checks.append(
                    MetricCheck(metric, year, wb_val, truth, "MISMATCH", wb_val - truth))

    for bad in (known_bad or []):
        try:
            wb_val = store.lookup(bad["metric"], bad["year"]).value
        except KeyError:
            wb_val = None
        tag = f"{bad['metric']}@{bad['year']}"
        # "caught" = the workbook does NOT equal the known-bad value AND differs from it
        if wb_val is None or not _close(wb_val, bad["bad"]):
            report.flagged_bad_caught.append(tag)
        else:
            report.flagged_bad_missed.append(tag)

    return report
