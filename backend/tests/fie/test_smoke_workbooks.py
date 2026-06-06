"""Task 0.8 — both delivered workbooks load; coverage is sane."""

import pytest

from app.engines.fie import FinancialFactStore


@pytest.mark.parametrize("name", ["millat_filled_fixed", "lucky_filled_fixed"])
def test_workbook_loads_and_covers(outputs_dir, name):
    import os
    s = FinancialFactStore.from_workbook(os.path.join(outputs_dir, f"{name}.xlsx"))
    cov = s.coverage()

    assert s.company  # resolved from manifest
    assert cov["years"][:5] == [2021, 2022, 2023, 2024, 2025]
    assert "forecasted" in set(s.findata["period_type"])
    assert cov["cells_parsed"] > 1000
    assert cov["source_ledger_rows"] > 0
    assert cov["insights"] > 0
    # no value should be silently zero-filled: rows_with_value < cells_parsed
    assert cov["rows_with_value"] < cov["cells_parsed"]


def test_insights_have_ids_and_fields(millat_store):
    ins = millat_store.insights(min_confidence=0.9)
    assert ins
    rec = ins[0]
    assert rec["insight_id"].startswith("INS")
    assert set(rec) >= {"year", "area", "takeaway", "confidence", "page"}
