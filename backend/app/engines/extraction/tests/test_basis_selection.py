"""Fragment-guarded basis preference in face-truth selection: for a standalone template,
prefer an unconsolidated candidate over a comparable consolidated/generic one, but never
let a mis-tagged unconsolidated FRAGMENT win over the real total."""
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.services.face_truth import build_face_truth


def _bs(title, metric, value, year=2022):
    return FinancialTable(
        statement_type=StatementType.balance_sheet, title=title,
        line_items=[LineItem(label=metric.replace("_", " "), canonical_metric=metric,
                             canonical_category="balance_sheet",
                             values=[LineItemValue(year=year, value=value)])])


def test_prefers_unconsolidated_over_comparable_consolidated():
    unc = _bs("Unconsolidated Statement of Financial Position", "equity", 7_022_757.0)
    con = _bs("Consolidated Statement of Financial Position", "equity", 9_053_339.0)
    ft = build_face_truth([unc, con])
    assert ft[("equity", 2022)][0] == 7_022_757.0


def test_fragment_not_preferred_over_real_total():
    # An unconsolidated SUBTOTAL ~8x below the real total must not win (fragment guard +
    # the magnitude-outlier filter both protect the real value).
    frag = _bs("Unconsolidated Statement of Financial Position", "total_assets", 3_960_000.0)
    p1 = _bs("Statement of Financial Position", "total_assets", 32_990_000.0)
    p2 = _bs("Consolidated Statement of Financial Position", "total_assets", 33_000_000.0)
    ft = build_face_truth([frag, p1, p2])
    assert ft[("total_assets", 2022)][0] > 30_000_000   # fragment rejected
