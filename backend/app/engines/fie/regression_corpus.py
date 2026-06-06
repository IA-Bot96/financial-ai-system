"""Fixed-query regression corpus.

A small, stable set of canonical queries with the intent each must route to, plus a
set of *generic invariants* every engine answer must satisfy (cited findings, valid
confidence band, resolvable citation handles). It is a behavioural tripwire: a
refactor that silently changes routing or breaks the citation contract trips here,
without pinning brittle exact-string golden answers.

  CASES            the corpus (query -> expected intent, with a short rationale)
  check_routing    assert a query routes to its expected intent (no data needed)
  check_invariants assert an engine Response upholds the cross-cutting contracts
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import understanding
from .models import Response

_HANDLE_RE = re.compile(r"\[(C\d+)\]")
_VALID_BANDS = {"High", "Medium", "Low"}


@dataclass(frozen=True)
class Case:
    query: str
    intent: str
    note: str = ""


# Canonical queries, one per supported intent (plus the ambiguous-profit case).
CASES: list[Case] = [
    Case("What is MTL's current ratio in 2024?", "ratio_analysis", "formula route"),
    Case("What was Millat's revenue in 2024?", "metric_lookup", "direct value"),
    Case("Show me Millat's revenue trend over the years", "trend_analysis", "multi-year"),
    Case("What are the key risks for MTL?", "risk_assessment", "qualitative"),
    Case("Compare MTL and Lucky on net margin", "peer_comparison", "two companies"),
    Case("Is MTL cheap on a P/E basis?", "valuation", "market-dependent"),
    Case("Latest news and announcements on MTL", "news_impact", "external news"),
    Case("What is MTL's dividend payout?", "dividend_analysis", "payouts"),
    Case("what was MTL's profit in 2024?", "metric_lookup", "ambiguous metric -> clarify"),
]


def check_routing(case: Case) -> list[str]:
    """Return a list of issues (empty == pass) for query->intent routing."""
    frame = understanding.build_frame(case.query)
    if frame.intent != case.intent:
        return [f"{case.query!r}: routed to {frame.intent!r}, expected {case.intent!r}"]
    return []


def check_invariants(resp: Response) -> list[str]:
    """Cross-cutting contracts every shipped answer must satisfy."""
    issues: list[str] = []
    if not isinstance(resp.direct_answer, str) or not resp.direct_answer.strip():
        issues.append("empty direct_answer")

    # confidence band (when scored) is from the known vocabulary
    if resp.confidence is not None and resp.confidence.band not in _VALID_BANDS:
        issues.append(f"invalid confidence band {resp.confidence.band!r}")

    # no citation, no claim — and every handle resolves to a shipped citation
    ref_ids = {c.ref_id for c in resp.citations}
    for f in resp.key_findings:
        handles = _HANDLE_RE.findall(f)
        if not handles:
            issues.append(f"uncited finding: {f!r}")
            continue
        dangling = [h for h in handles if h not in ref_ids]
        if dangling:
            issues.append(f"dangling citation {dangling} in finding: {f!r}")
    return issues


def run(engine) -> dict:
    """Run the full corpus against a live engine. Returns
    {passed, failed, issues:[...]} — usable as an eval gate or a smoke check."""
    issues: list[str] = []
    for case in CASES:
        issues += check_routing(case)
        try:
            resp = engine.answer(case.query)
        except Exception as e:
            issues.append(f"{case.query!r}: engine raised {type(e).__name__}: {e}")
            continue
        issues += [f"{case.query!r}: {m}" for m in check_invariants(resp)]
    return {"passed": len(CASES) - len(issues), "failed": len(issues), "issues": issues}
