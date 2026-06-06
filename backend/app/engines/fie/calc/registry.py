"""Declarative formula registry (L4 / deliverable #7).

Formulas are data, not code: each FormulaSpec carries an arithmetic ``expression``
over named input keys, a list of typed inputs (metric + year offset), and string
``domain_guards``. A small safe AST evaluator computes expressions and guards —
no eval(), only whitelisted arithmetic/comparison nodes.

See docs/fie_implementation_plan.md §Phase 2 (2.1) and architecture §5.2/§5.3.
"""

from __future__ import annotations

import ast
import operator
from typing import Literal, Optional

from pydantic import BaseModel, Field

Category = Literal[
    "growth", "profitability", "liquidity", "leverage", "cashflow",
    "valuation", "forecast",
]
OutputUnit = Literal["ratio", "percent", "currency", "x"]


class FormulaInput(BaseModel):
    key: str                       # name used in the expression
    metric: str                    # canonical metric id resolved via the store
    year_offset: int = 0           # 0 = target year, -1 = prior year, ...
    required: bool = True


class FormulaSpec(BaseModel):
    id: str
    category: Category
    expression: str                # e.g. "(rev_t - rev_t1) / rev_t1"
    inputs: list[FormulaInput]
    domain_guards: list[str] = Field(default_factory=list)  # e.g. ["rev_t1 != 0"]
    output_unit: OutputUnit = "ratio"
    rounding: int = 4
    version: str = "1.0"
    description: str = ""


# --- safe expression evaluator --------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}
_CMP_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
}
_FUNCS = {"abs": abs, "min": min, "max": max}


class FormulaError(Exception):
    pass


def _eval_node(node: ast.AST, values: dict[str, float]):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, values)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval_node(node.left, values)
        right = _eval_node(node.right, values)
        if isinstance(node.op, ast.Div) and right == 0:
            raise FormulaError("division by zero")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_node(node.operand, values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left = _eval_node(node.left, values)
        right = _eval_node(node.comparators[0], values)
        return _CMP_OPS[type(node.ops[0])](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        args = [_eval_node(a, values) for a in node.args]
        return _FUNCS[node.func.id](*args)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise FormulaError(f"unknown identifier {node.id!r}")
        return values[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    raise FormulaError(f"unsupported expression node: {ast.dump(node)}")


def safe_eval(expr: str, values: dict[str, float]):
    """Evaluate an arithmetic/comparison expression over ``values`` safely."""
    tree = ast.parse(expr, mode="eval")
    return _eval_node(tree, values)


# --- seed registry ---------------------------------------------------------

def _i(key, metric, off=0, required=True):
    return FormulaInput(key=key, metric=metric, year_offset=off, required=required)


_SPECS: list[FormulaSpec] = [
    # growth
    FormulaSpec(id="revenue_growth", category="growth", output_unit="percent",
                expression="(rev_t - rev_t1) / rev_t1", domain_guards=["rev_t1 != 0"],
                inputs=[_i("rev_t", "revenue"), _i("rev_t1", "revenue", -1)],
                description="YoY revenue growth"),
    FormulaSpec(id="earnings_growth", category="growth", output_unit="percent",
                expression="(pat_t - pat_t1) / pat_t1", domain_guards=["pat_t1 != 0"],
                inputs=[_i("pat_t", "pat"), _i("pat_t1", "pat", -1)],
                description="YoY earnings (PAT) growth"),
    # profitability
    FormulaSpec(id="gross_margin", category="profitability", output_unit="percent",
                expression="gross_profit / revenue", domain_guards=["revenue != 0"],
                inputs=[_i("gross_profit", "gross_profit"), _i("revenue", "revenue")]),
    FormulaSpec(id="operating_margin", category="profitability", output_unit="percent",
                expression="operating_profit / revenue", domain_guards=["revenue != 0"],
                inputs=[_i("operating_profit", "operating_profit"), _i("revenue", "revenue")]),
    FormulaSpec(id="net_margin", category="profitability", output_unit="percent",
                expression="pat / revenue", domain_guards=["revenue != 0"],
                inputs=[_i("pat", "pat"), _i("revenue", "revenue")]),
    FormulaSpec(id="roe", category="profitability", output_unit="percent",
                expression="pat / ((eq_t + eq_t1) / 2)", domain_guards=["(eq_t + eq_t1) != 0"],
                inputs=[_i("pat", "pat"), _i("eq_t", "total_equity"),
                        _i("eq_t1", "total_equity", -1)],
                description="Return on average equity"),
    FormulaSpec(id="roa", category="profitability", output_unit="percent",
                expression="pat / ((ta_t + ta_t1) / 2)", domain_guards=["(ta_t + ta_t1) != 0"],
                inputs=[_i("pat", "pat"), _i("ta_t", "total_assets"),
                        _i("ta_t1", "total_assets", -1)],
                description="Return on average assets"),
    # liquidity
    FormulaSpec(id="current_ratio", category="liquidity", output_unit="x",
                expression="current_assets / current_liabilities",
                domain_guards=["current_liabilities != 0"],
                inputs=[_i("current_assets", "current_assets"),
                        _i("current_liabilities", "current_liabilities")]),
    FormulaSpec(id="quick_ratio", category="liquidity", output_unit="x",
                expression="(current_assets - stock_in_trade) / current_liabilities",
                domain_guards=["current_liabilities != 0"],
                inputs=[_i("current_assets", "current_assets"),
                        _i("stock_in_trade", "stock_in_trade", required=False),
                        _i("current_liabilities", "current_liabilities")]),
    # liquidity (cont.)
    FormulaSpec(id="cash_ratio", category="liquidity", output_unit="x",
                expression="cash_and_bank / current_liabilities",
                domain_guards=["current_liabilities != 0"],
                inputs=[_i("cash_and_bank", "cash_and_bank"),
                        _i("current_liabilities", "current_liabilities")]),
    # leverage
    FormulaSpec(id="debt_to_equity", category="leverage", output_unit="x",
                expression="(non_current_liabilities + current_liabilities) / total_equity",
                domain_guards=["total_equity != 0"],
                inputs=[_i("non_current_liabilities", "non_current_liabilities"),
                        _i("current_liabilities", "current_liabilities"),
                        _i("total_equity", "total_equity")]),
    FormulaSpec(id="debt_to_assets", category="leverage", output_unit="x",
                expression="(non_current_liabilities + current_liabilities) / total_assets",
                domain_guards=["total_assets != 0"],
                inputs=[_i("non_current_liabilities", "non_current_liabilities"),
                        _i("current_liabilities", "current_liabilities"),
                        _i("total_assets", "total_assets")]),
    FormulaSpec(id="equity_multiplier", category="leverage", output_unit="x",
                expression="total_assets / total_equity", domain_guards=["total_equity != 0"],
                inputs=[_i("total_assets", "total_assets"), _i("total_equity", "total_equity")]),
    FormulaSpec(id="interest_coverage", category="leverage", output_unit="x",
                expression="operating_profit / abs(finance_cost)",
                domain_guards=["finance_cost != 0"],
                inputs=[_i("operating_profit", "operating_profit"),
                        _i("finance_cost", "finance_cost")]),
    # efficiency
    FormulaSpec(id="asset_turnover", category="profitability", output_unit="x",
                expression="revenue / total_assets", domain_guards=["total_assets != 0"],
                inputs=[_i("revenue", "revenue"), _i("total_assets", "total_assets")]),
    # cash flow
    FormulaSpec(id="free_cash_flow", category="cashflow", output_unit="currency",
                expression="operating_cash_flow + capex",  # capex stored as a negative outflow
                inputs=[_i("operating_cash_flow", "operating_cash_flow"),
                        _i("capex", "capex")],
                description="Operating cash flow less capital expenditure"),
    # valuation building blocks (internal; market-dependent ratios live in the engine)
    FormulaSpec(id="ebitda", category="valuation", output_unit="currency",
                expression="operating_profit + depreciation + amortization",
                inputs=[_i("operating_profit", "operating_profit"),
                        _i("depreciation", "depreciation_expense"),
                        _i("amortization", "amortization_expense", required=False)],
                description="EBITDA ~= operating profit + D&A"),
    FormulaSpec(id="book_value_per_share", category="valuation", output_unit="currency",
                expression="total_equity / shares_outstanding",
                domain_guards=["shares_outstanding != 0"],
                inputs=[_i("total_equity", "total_equity"),
                        _i("shares_outstanding", "shares_outstanding")]),
    FormulaSpec(id="eps_computed", category="valuation", output_unit="currency",
                expression="pat / shares_outstanding", domain_guards=["shares_outstanding != 0"],
                inputs=[_i("pat", "pat"), _i("shares_outstanding", "shares_outstanding")]),
    # forecast validation
    FormulaSpec(id="forecast_error", category="forecast", output_unit="percent",
                expression="(actual - forecast) / forecast", domain_guards=["forecast != 0"],
                inputs=[_i("actual", "revenue"), _i("forecast", "revenue_forecast")],
                description="Actual vs forecast variance (metric-parameterized later)"),
]

REGISTRY: dict[str, FormulaSpec] = {s.id: s for s in _SPECS}


def get(formula_id: str) -> Optional[FormulaSpec]:
    return REGISTRY.get(formula_id)


def register(spec: FormulaSpec) -> None:
    REGISTRY[spec.id] = spec
