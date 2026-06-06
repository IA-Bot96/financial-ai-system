"""Eval harness (Phase 5.3).

A golden-query regression set + a runner that checks expectations per query and
reports per-intent metrics. Used in CI to catch regressions across the whole
answer path. Not on the runtime answer path.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Each case: query + expectations. ``expect`` keys are optional checks.
GOLDEN_QUERIES: list[dict] = [
    {"query": "current ratio for MTL 2024", "expect": {
        "intent": "ratio_analysis", "has_value": True, "approx": 1.24,
        "min_citations": 1}},
    {"query": "gross margin for millat 2025", "expect": {
        "intent": "ratio_analysis", "has_value": True, "approx": 0.2661}},
    {"query": "debt to equity for millat 2024", "expect": {
        "intent": "ratio_analysis", "has_value": True, "min_citations": 1}},
    {"query": "what was revenue for MTL in 2025?", "expect": {
        "intent": "metric_lookup", "answer_contains": "52,108,997"}},
    {"query": "what are the key risks for MTL?", "expect": {
        "intent": "risk_assessment", "min_findings": 1}},
    {"query": "current ratio MTL vs Lucky 2024", "expect": {
        "intent": "peer_comparison", "answer_contains": "Lucky"}},
    {"query": "is my 2026 revenue forecast for MTL still valid?", "expect": {
        "intent": "forecast_validation"}},
]


@dataclass
class CaseResult:
    query: str
    intent: str
    passed: bool
    failures: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    def metrics(self) -> dict:
        by_intent: dict[str, dict] = {}
        for c in self.cases:
            d = by_intent.setdefault(c.intent, {"total": 0, "passed": 0})
            d["total"] += 1
            d["passed"] += int(c.passed)
        return {
            "cases": len(self.cases),
            "passed": sum(c.passed for c in self.cases),
            "by_intent": by_intent,
        }


def _check(resp, expect: dict) -> list[str]:
    fails: list[str] = []
    if "intent" in expect:
        # intent is not on Response; inferred via calcs/answer — checked by caller
        pass
    if expect.get("has_value"):
        if not (resp.calculations and resp.calculations[0].value is not None):
            fails.append("expected a computed value")
    if "approx" in expect and resp.calculations and resp.calculations[0].value is not None:
        got = resp.calculations[0].value
        if abs(got - expect["approx"]) > 0.01 * max(abs(expect["approx"]), 1.0):
            fails.append(f"value {got} != approx {expect['approx']}")
    if "min_citations" in expect and len(resp.citations) < expect["min_citations"]:
        fails.append(f"citations {len(resp.citations)} < {expect['min_citations']}")
    if "min_findings" in expect and len(resp.key_findings) < expect["min_findings"]:
        fails.append(f"findings {len(resp.key_findings)} < {expect['min_findings']}")
    if "answer_contains" in expect and expect["answer_contains"] not in resp.direct_answer:
        fails.append(f"answer missing {expect['answer_contains']!r}")
    return fails


def run_eval(engine, cases: list[dict] | None = None) -> EvalReport:
    from .. import understanding
    report = EvalReport()
    for case in (cases or GOLDEN_QUERIES):
        q = case["query"]
        expect = case.get("expect", {})
        frame = understanding.understand(q, llm=getattr(engine, "llm", None))
        resp = engine.answer(q)
        fails = _check(resp, expect)
        if "intent" in expect and frame.intent != expect["intent"]:
            fails.append(f"intent {frame.intent} != {expect['intent']}")
        report.cases.append(CaseResult(query=q, intent=frame.intent,
                                       passed=not fails, failures=fails))
    return report
