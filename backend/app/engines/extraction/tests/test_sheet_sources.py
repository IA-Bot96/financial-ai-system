"""sheet_sources provenance map: sheet -> [{report_file, pages, table_ids, weight}]."""
from types import SimpleNamespace

from app.engines.extraction.services.provenance import build_sheet_sources


def _src(report_file, pages, table_id):
    return SimpleNamespace(report_file=report_file, pages=pages, table_id=table_id)


def test_template_aggregates_writes_and_overrides():
    # Two detail-sheet writes (one sheet, same file, pages 12 & 13) + a write with no source.
    plan = SimpleNamespace(writes=[
        SimpleNamespace(sheet="BS1", source=_src("2024.pdf", [12], "2024.pdf:p12:t0")),
        SimpleNamespace(sheet="BS1", source=_src("2024.pdf", [13], "2024.pdf:p13:t1")),
        SimpleNamespace(sheet="BS1", source=None),                       # skipped
    ])
    # Headline output sheet "BS" is formula-driven -> only the override carries its page.
    overrides = [SimpleNamespace(sheet="BS", report_file="2024.pdf", page=12,
                                 table_id="2024.pdf:p12:t0")]

    out = build_sheet_sources(mode="template", plan=plan, overrides=overrides)

    assert out["BS1"] == [{
        "report_file": "2024.pdf", "pages": [12, 13],
        "table_ids": ["2024.pdf:p12:t0", "2024.pdf:p13:t1"], "weight": 2,
    }]
    assert out["BS"] == [{
        "report_file": "2024.pdf", "pages": [12],
        "table_ids": ["2024.pdf:p12:t0"], "weight": 1,
    }]


def test_template_ranks_multi_report_by_weight():
    plan = SimpleNamespace(writes=[
        SimpleNamespace(sheet="P&L", source=_src("2023.pdf", [9], "t-a")),
        SimpleNamespace(sheet="P&L", source=_src("2024.pdf", [9], "t-b")),
        SimpleNamespace(sheet="P&L", source=_src("2024.pdf", [10], "t-c")),
    ])
    out = build_sheet_sources(mode="template", plan=plan, overrides=[])
    # 2024.pdf contributed 2 cells -> ranked first; the UI defaults to entries[0].
    assert [e["report_file"] for e in out["P&L"]] == ["2024.pdf", "2023.pdf"]
    assert out["P&L"][0]["pages"] == [9, 10] and out["P&L"][0]["weight"] == 2


def _table(title, st_value, consolidated, source):
    return SimpleNamespace(title=title, statement_type=SimpleNamespace(value=st_value),
                           consolidated=consolidated, source=source)


def test_no_template_one_entry_per_sheet_with_writer_naming():
    tables = [
        _table("Notes", "balance_sheet", None, _src("2024.pdf", [12], "t0")),
        # Same title -> writer dedups the second sheet name (" 2"); we mirror that.
        _table("Notes", "balance_sheet", None, _src("2024.pdf", [40], "t1")),
        _table(None, "income_statement", None, None),  # no source -> no entry, name still consumed
    ]
    out = build_sheet_sources(mode="no_template", tables=tables)

    assert out["Notes"][0]["pages"] == [12]
    assert out["Notes 2"][0]["pages"] == [40]           # dedup suffix mirrors the writer
    # The source-less income statement sheet contributes no provenance entry.
    assert "Income Statement" not in out


def _li(values):
    return SimpleNamespace(values=values)


def _val(source):
    return SimpleNamespace(source=source)


def test_no_template_aggregates_value_level_sources():
    # Real post-multiyear shape: NO table-level source; provenance lives on each value.
    # A merged multi-year table spans several pages -> all should be collected.
    t = SimpleNamespace(
        title="Profit & Loss", statement_type=SimpleNamespace(value="income_statement"),
        consolidated=None, source=None,
        line_items=[
            _li([_val(_src("2024.pdf", [9], "2024.pdf:p9:t0")),
                 _val(_src("2024.pdf", [10], "2024.pdf:p10:t1"))]),
            _li([_val(_src("2023.pdf", [9], "2023.pdf:p9:t0")),
                 _val(None)]),                              # missing source skipped
        ],
    )
    out = build_sheet_sources(mode="no_template", tables=[t])

    entries = out["Profit & Loss"]
    # 2024.pdf contributed 2 values -> ranked first; pages unioned across the table.
    assert [e["report_file"] for e in entries] == ["2024.pdf", "2023.pdf"]
    assert entries[0]["pages"] == [9, 10] and entries[0]["weight"] == 2
    assert entries[1]["report_file"] == "2023.pdf" and entries[1]["pages"] == [9]


def test_empty_inputs_return_empty_map():
    assert build_sheet_sources(mode="template", plan=None, overrides=None) == {}
    assert build_sheet_sources(mode="no_template", tables=[]) == {}
