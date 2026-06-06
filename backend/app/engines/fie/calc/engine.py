"""Calculation engine (L4).

Derives metrics from authoritative workbook values (architecture §5.1: trust the
workbook, derive the rest). Backed by the declarative FormulaSpec registry.

For each input the engine resolves (metric, year + year_offset) to a FactRef via
the store, prefers a stored value over recomputation, enforces domain guards,
attaches all input citations, and sets result confidence bottom-up.

See docs/fie_implementation_plan.md §Phase 2 (2.2).
"""

from __future__ import annotations

from typing import Optional

from ..models import CalcResult, FactRef
from . import registry
from .registry import FormulaError, FormulaSpec, safe_eval


class CalcEngine:
    def __init__(self, store, formulas: dict | None = None) -> None:
        self.store = store
        self._registry = formulas if formulas is not None else registry.REGISTRY

    def evaluate(self, formula_id: str, year: int, *, company: str | None = None
                 ) -> CalcResult:
        spec: Optional[FormulaSpec] = self._registry.get(formula_id)
        if spec is None:
            return CalcResult(formula_id=formula_id, value=None, confidence="Low",
                              note=f"unknown formula {formula_id!r}")

        values: dict[str, float] = {}
        facts: list[FactRef] = []
        citations = []
        missing: list[str] = []

        for inp in spec.inputs:
            fact = self._resolve(inp.metric, year + inp.year_offset)
            if fact is None or fact.value is None:
                if inp.required:
                    missing.append(f"{inp.metric}@{year + inp.year_offset}")
                else:
                    values[inp.key] = 0.0  # optional input absent -> neutral in expression
                continue
            values[inp.key] = fact.value
            facts.append(fact)
            citations.extend(self.store.cite(fact))

        if missing:
            return CalcResult(
                formula_id=formula_id, value=None, unit=spec.output_unit,
                inputs=facts, citations=_dedupe(citations), confidence="Low",
                expression=spec.expression, note=f"missing inputs: {', '.join(missing)}",
            )

        # domain guards
        for guard in spec.domain_guards:
            try:
                if not safe_eval(guard, values):
                    return CalcResult(
                        formula_id=formula_id, value=None, unit=spec.output_unit,
                        inputs=facts, citations=_dedupe(citations), confidence="Low",
                        expression=spec.expression, note=f"domain guard failed: {guard}",
                    )
            except FormulaError as e:
                return CalcResult(
                    formula_id=formula_id, value=None, unit=spec.output_unit,
                    inputs=facts, citations=_dedupe(citations), confidence="Low",
                    expression=spec.expression, note=f"guard error: {e}",
                )

        try:
            value = round(safe_eval(spec.expression, values), spec.rounding)
        except FormulaError as e:
            return CalcResult(
                formula_id=formula_id, value=None, unit=spec.output_unit,
                inputs=facts, citations=_dedupe(citations), confidence="Low",
                expression=spec.expression, note=f"eval error: {e}",
            )

        return CalcResult(
            formula_id=formula_id, value=value, unit=spec.output_unit,
            inputs=facts, citations=_dedupe(citations),
            confidence=self._confidence(facts), expression=spec.expression,
        )

    def _resolve(self, metric: str, year: int) -> Optional[FactRef]:
        """Prefer a stored headline value; fall back to detail. (§5.1 prefer-stored)"""
        for level in ("headline", "detail"):
            try:
                return self.store.lookup(metric, year, level=level)
            except KeyError:
                continue
        return None

    @staticmethod
    def _confidence(facts: list[FactRef]) -> str:
        """Bottom-up: High if every input is citeable; Medium otherwise.

        No financial-mismatch cap at runtime (workbook trusted, architecture §0.3).
        """
        if facts and all(f.provenance_basis in ("direct", "via_detail", "workbook") for f in facts):
            return "High"
        return "Medium"


def _dedupe(citations):
    seen, out = set(), []
    for c in citations:
        loc = c.locator or {}
        key = (loc.get("report_file"), loc.get("page"), loc.get("table_id"))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
