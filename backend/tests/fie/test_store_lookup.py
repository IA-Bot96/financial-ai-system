"""Task 0.5 — store assembly + lookups against the real workbook."""


def test_lookup_revenue_2024_restated(millat_store):
    f = millat_store.lookup("revenue", 2024)
    assert f.value == 91_534_501.0
    assert f.level == "headline"
    assert f.period_type == "historical"
    assert (f.sheet, f.cell) == ("P&L", "F6")


def test_lookup_revenue_2025_face_truth(millat_store):
    assert millat_store.lookup("revenue", 2025).value == 52_108_997.0


def test_forecast_period_slot_exists(millat_store):
    f = millat_store.lookup("revenue", 2026, period_type="forecasted")
    assert f.period_type == "forecasted"  # structure captured even if value is None


def test_years_discovered(millat_store):
    assert millat_store.years == [2021, 2022, 2023, 2024, 2025, 2026, 2027, 2028, 2029, 2030]


def test_detail_lookup(millat_store):
    det = millat_store.detail(sheet="PL1 - Revenue", year=2024)
    assert len(det) > 0
    assert (det["level"] == "detail").all()
