"""Contract-integrity boot check (fail-fast wiring validation).

Ported in spirit from the legacy startup self-test: before the engine serves any
query, assert that the *internal contracts* between modules are intact, so a config
hole (a formula that references an undeclared input, an unregistered formula id, a
non-canonical metric, a taxonomy theme pointing at a missing category, a broken
authority matrix, a scrambled citation-precision order) fails LOUDLY at boot rather
than producing a wrong-but-confident answer at query time.

``verify_contracts()`` returns a list of ``CheckResult`` (every check, ok or not).
``assert_contracts()`` runs them and raises ``ContractError`` on the first failure —
call it at process/app startup.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from . import authority, citation_enforce
from .calc import registry as formula_registry
from .ontology import MetricOntology
from .understanding import _FORMULA_KEYWORDS

# formula-input metrics that are intentionally NOT statement-line canonicals
# (forecast placeholders resolved from a forecast source, not the workbook).
_NON_CANONICAL_INPUTS = {"revenue_forecast"}

# formulas the engine computes inline (market-dependent), not via the calc registry.
_ENGINE_FORMULAS = {"pe_ratio", "pb_ratio", "ev_ebitda"}

# identifiers a formula expression may reference beyond its declared inputs.
_EXPR_BUILTINS = set(formula_registry._FUNCS)  # abs / min / max


class ContractError(RuntimeError):
    """A boot-time contract is violated — the engine is mis-wired."""


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


def _names_in(expr: str) -> set[str]:
    """All identifier names referenced in an arithmetic/guard expression."""
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval"))
            if isinstance(n, ast.Name)}


def _check_authority_matrix() -> CheckResult:
    try:
        authority.validate_matrix()
        return CheckResult("authority_matrix", True, "every claim type ranked")
    except Exception as e:  # pragma: no cover - defensive
        return CheckResult("authority_matrix", False, str(e))


def _check_formula_expressions() -> CheckResult:
    """Every formula expression + domain guard references only declared inputs
    (plus whitelisted builtins) — so safe_eval can never hit an unknown identifier."""
    bad: list[str] = []
    for spec in formula_registry.REGISTRY.values():
        declared = {i.key for i in spec.inputs} | _EXPR_BUILTINS
        try:
            refs = _names_in(spec.expression)
            for g in spec.domain_guards:
                refs |= _names_in(g)
        except SyntaxError as e:
            bad.append(f"{spec.id}: unparseable expression ({e})")
            continue
        undeclared = refs - declared
        if undeclared:
            bad.append(f"{spec.id}: undeclared {sorted(undeclared)}")
    return CheckResult("formula_expressions", not bad,
                       "; ".join(bad) or f"{len(formula_registry.REGISTRY)} formulas ok")


def _check_understanding_formulas_registered() -> CheckResult:
    """Every formula id the query-understanding layer can emit is computable —
    either in the calc registry or handled inline by the engine."""
    known = set(formula_registry.REGISTRY) | _ENGINE_FORMULAS
    emitted = {fid for _, fid, _ in _FORMULA_KEYWORDS}
    missing = sorted(emitted - known)
    return CheckResult("understanding_formulas_registered", not missing,
                       f"unregistered: {missing}" if missing else f"{len(emitted)} ids ok")


def _check_formula_inputs_canonical() -> CheckResult:
    """Every formula input resolves to a known canonical metric id (or an
    explicitly-allowed forecast placeholder) — no formula depends on a phantom metric."""
    ont = MetricOntology()
    canon = set(ont._by_canonical.keys()) | _NON_CANONICAL_INPUTS
    bad: list[str] = []
    for spec in formula_registry.REGISTRY.values():
        for i in spec.inputs:
            if i.metric not in canon:
                bad.append(f"{spec.id}.{i.key} -> {i.metric}")
    return CheckResult("formula_inputs_canonical", not bad,
                       "; ".join(bad) or "all inputs canonical")


def _check_citation_precision_order() -> CheckResult:
    """Precision ranks are strictly ordered CELL > PAGE > REF > NONE and the ship
    floor is REF — the 'no citation, no claim' gate depends on this ordering."""
    r = citation_enforce._RANK
    ok = (r["CELL"] > r["PAGE"] > r["REF"] > r["NONE"]
          and citation_enforce._MIN_OK == r["REF"])
    return CheckResult("citation_precision_order", ok,
                       f"ranks={r} min_ok={citation_enforce._MIN_OK}")


def _check_taxonomy() -> CheckResult:
    """Qualitative taxonomy loads and every theme points at an existing category."""
    try:
        from . import qualitative
        tax = qualitative._taxonomy()
    except Exception as e:
        return CheckResult("taxonomy", False, f"load failed: {e}")
    cats, themes = tax["categories"], tax["themes"]
    if not cats or not themes:
        return CheckResult("taxonomy", False, "empty categories/themes")
    orphans = [ref for ref, t in themes.items()
               if t.get("category_ref") not in cats]
    return CheckResult("taxonomy", not orphans,
                       f"orphan themes: {orphans}" if orphans
                       else f"{len(cats)} categories / {len(themes)} themes")


_CHECKS = (
    _check_authority_matrix,
    _check_formula_expressions,
    _check_understanding_formulas_registered,
    _check_formula_inputs_canonical,
    _check_citation_precision_order,
    _check_taxonomy,
)


def verify_contracts() -> list[CheckResult]:
    """Run every contract check; return all results (does not raise)."""
    return [check() for check in _CHECKS]


def assert_contracts() -> list[CheckResult]:
    """Run every contract check; raise ContractError on the first failure.
    Call at app/process startup so a mis-wire fails fast and visibly."""
    results = verify_contracts()
    failed = [r for r in results if not r.ok]
    if failed:
        lines = "\n".join(f"  - {r.name}: {r.detail}" for r in failed)
        raise ContractError(f"FIE contract integrity check failed:\n{lines}")
    return results
