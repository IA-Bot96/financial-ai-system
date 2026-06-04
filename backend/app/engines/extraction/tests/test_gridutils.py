"""Tests for the pure grid helpers (orientation, clustering, years, continuation)."""
from app.engines.extraction.models.table import TableOrientation
from app.engines.extraction.pipeline import gridutils as gu


def test_vertical_orientation_is_canonical():
    rows = [
        ["", "2024", "2025"],
        ["Revenue", "100", "120"],
        ["Net income", "10", "15"],
    ]
    out, orient = gu.detect_and_normalize(rows)
    assert orient == TableOrientation.vertical
    assert out[0] == ["", "2024", "2025"]


def test_horizontal_table_is_transposed():
    # Years run DOWN the first column -> should be transposed to canonical.
    rows = [
        ["Year", "Revenue", "Net income"],
        ["2024", "100", "10"],
        ["2025", "120", "15"],
    ]
    out, orient = gu.detect_and_normalize(rows)
    assert orient == TableOrientation.horizontal
    # After transpose, the header row carries the years.
    assert gu.extract_years(out[0]) == [2024, 2025]


def test_extract_years():
    assert gu.extract_years(["", "FY2023", "2024 restated"]) == [2023, 2024]


def test_detect_currency_unit():
    cur, unit = gu.detect_currency_unit("Consolidated balance sheet (USD '000)")
    assert cur == "USD"
    assert unit == "thousands"


def test_looks_tabular_rejects_prose():
    prose = [["The company performed well this year and grew."]]
    assert gu.looks_tabular(prose) is False


def test_looks_tabular_accepts_numeric_grid():
    grid = [["", "2024", "2025"], ["Revenue", "100", "120"]]
    assert gu.looks_tabular(grid) is True


def test_cluster_words_to_grid():
    words = [
        {"text": "Revenue", "x0": 0, "x1": 50, "top": 0, "bottom": 10},
        {"text": "100", "x0": 200, "x1": 230, "top": 0, "bottom": 10},
        {"text": "120", "x0": 400, "x1": 430, "top": 0, "bottom": 10},
        {"text": "Costs", "x0": 0, "x1": 50, "top": 30, "bottom": 40},
        {"text": "40", "x0": 200, "x1": 220, "top": 30, "bottom": 40},
        {"text": "50", "x0": 400, "x1": 420, "top": 30, "bottom": 40},
    ]
    grid = gu.cluster_words_to_grid(words, row_tol=8, col_tol=25)
    assert grid == [["Revenue", "100", "120"], ["Costs", "40", "50"]]


def test_is_continuation_true_for_next_page_no_header():
    prev = [["", "2024", "2025"], ["Revenue", "100", "120"]]
    cur = [["Other income", "5", "6"]]  # no year header, same col count
    assert gu.is_continuation(prev, [4], cur, [5], same_section=True) is True


def test_is_continuation_false_when_new_year_header():
    prev = [["", "2024", "2025"], ["Revenue", "100", "120"]]
    cur = [["", "2024", "2025"], ["Cash", "9", "9"]]  # fresh header -> new table
    assert gu.is_continuation(prev, [4], cur, [5], same_section=True) is False


def test_is_continuation_false_when_page_gap():
    prev = [["", "2024"], ["Revenue", "100"]]
    cur = [["Other", "5"]]
    assert gu.is_continuation(prev, [4], cur, [6], same_section=True) is False


def test_multiword_label_stays_in_one_cell():
    # Mimics a real statement row: wide multi-word label + note + two values.
    # Words are spaced far apart (would fragment under fixed-gap clustering).
    def w(t, x0, x1, top):
        return {"text": t, "x0": x0, "x1": x1, "top": top}

    words = [
        # header year row
        w("2025", 410, 440, 0), w("2024", 500, 530, 0),
        w("Revenue", 40, 90, 30), w("from", 95, 120, 30), w("contracts", 125, 175, 30),
        w("34", 346, 356, 30), w("53,347", 410, 455, 30), w("95,020", 500, 545, 30),
        w("Cost", 40, 70, 60), w("of", 75, 85, 60), w("sales", 90, 120, 60),
        w("35", 346, 356, 60), w("38,940", 410, 455, 60), w("71,048", 500, 545, 60),
        w("Gross", 40, 75, 90), w("profit", 80, 120, 90),
        w("14,407", 410, 455, 90), w("23,971", 500, 545, 90),
    ]
    grid = gu.cluster_words_to_grid(words, row_tol=8, col_tol=25)
    labels = [r[0] for r in grid]
    assert "Revenue from contracts" in labels
    assert "Cost of sales" in labels
    assert gu.has_labels(grid) is True


def test_has_labels_false_for_numbers_only():
    grid = [["1,234", "5,678"], ["(90)", "12"]]
    assert gu.has_labels(grid) is False
