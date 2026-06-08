"""Per-sheet source provenance.

Maps each user-facing output worksheet to the PDF file(s) and 1-based page(s)
its data came from, so a UI showing the workbook and the source PDF side by side
can jump the PDF to the right page when the user switches sheets
(e.g. open "BS" tab -> scroll PDF to the balance-sheet page).

Built entirely from lineage the pipeline already carries (`SourceRef` on every
`CellWrite` and every extracted table) — no engine/orchestrator changes. The
result is emitted into the run manifest as `sheet_sources`:

    {
      "BS":  [{"report_file": "2024.pdf", "pages": [12, 13], "table_ids": [...], "weight": 9}],
      "P&L": [{"report_file": "2024.pdf", "pages": [9],      "table_ids": [...], "weight": 7}],
      ...
    }

Entries per sheet are ranked by `weight` (number of contributing cells/tables),
so a UI can default to `sheet_sources[sheet][0]`. Multiple entries occur when a
sheet's data spans several reports — key on (report_file, page), not page alone.
"""
from __future__ import annotations


def _accumulate(acc: dict, sheet, report_file, pages, table_id, weight: int = 1) -> None:
    if not sheet or not report_file:
        return  # nothing to point the UI at
    ent = acc.setdefault(sheet, {}).setdefault(
        report_file, {"pages": set(), "table_ids": set(), "weight": 0})
    for p in (pages or []):
        if isinstance(p, int):
            ent["pages"].add(p)
    if table_id:
        ent["table_ids"].add(table_id)
    ent["weight"] += weight


def _finalize(acc: dict) -> dict:
    out: dict = {}
    for sheet, by_file in acc.items():
        entries = [
            {"report_file": rf, "pages": sorted(e["pages"]),
             "table_ids": sorted(e["table_ids"]), "weight": e["weight"]}
            for rf, e in by_file.items()
        ]
        # Most-contributing source first; the UI can default to entries[0].
        entries.sort(key=lambda e: (-e["weight"], e["report_file"]))
        out[sheet] = entries
    return out


_PROP_NAME = "SheetSources"


def embed_sheet_sources(output_path, sheet_sources: dict) -> None:
    """Embed the sheet -> source-page map INTO the workbook as a custom document property
    (docProps/custom.xml), so it travels with the .xlsx itself.

    Why a custom property and not a sheet: the viewer opens the workbook file directly
    (POST /api/fie/sessions), and its per-sheet listing enumerates every WORKSHEET — a
    helper sheet would show up as a junk tab. A custom doc property is invisible to that
    listing and to the financial-metric extraction, is preserved by the frontend's
    round-trip save (untouched docProps part), and survives download/re-upload. The value
    is the same JSON as the manifest's `sheet_sources`.
    """
    if not sheet_sources:
        return
    import json as _json

    from openpyxl import load_workbook
    from openpyxl.packaging.custom import CustomPropertyList, StringProperty

    wb = load_workbook(output_path, data_only=False)   # keep formulas/cached values intact
    try:
        # Rebuild the list dropping any prior value (CustomPropertyList has no delete()).
        existing = wb.custom_doc_props
        props = CustomPropertyList()
        for p in (existing or []):
            if p.name != _PROP_NAME:
                props.append(p)
        props.append(StringProperty(name=_PROP_NAME, value=_json.dumps(sheet_sources)))
        wb.custom_doc_props = props
        wb.save(output_path)
    finally:
        wb.close()


def build_sheet_sources(*, mode: str, plan=None, overrides=None, tables=None) -> dict:
    """Build the sheet -> [provenance] map for the given output mode.

    template     : aggregate `plan.writes` (detail/breakdown sheets) + headline
                   `overrides` (the formula-driven output sheets like BS/P&L,
                   which carry no plan writes but do carry audited-substitution
                   provenance).
    no_template  : one sheet per table, named exactly as the writer names it.
    """
    acc: dict = {}
    if mode == "template":
        for w in (getattr(plan, "writes", None) or []):
            src = getattr(w, "source", None)
            if src is None:
                continue
            _accumulate(acc, getattr(w, "sheet", None), getattr(src, "report_file", None),
                        getattr(src, "pages", None), getattr(src, "table_id", None))
        # Headline OUTPUT sheets (BS, P&L) are formula-driven, so they have no
        # plan.writes — but audited substitutions land directly in their cells and
        # DO carry provenance. Surface it so the headline tab maps to a page too.
        for o in (overrides or []):
            page = getattr(o, "page", None)
            _accumulate(acc, getattr(o, "sheet", None), getattr(o, "report_file", None),
                        [page] if isinstance(page, int) else [], getattr(o, "table_id", None))
    else:  # no_template: one sheet per table, named exactly as excel_writer names it
        from app.engines.extraction.pipeline.excel_writer import _sheet_name, _table_title
        used: set = set()
        for t in (tables or []):
            name = _sheet_name(_table_title(t), used)   # consume name even if no source
            # A merged multi-year table carries NO table-level source (resolve_multiyear
            # builds it fresh) — its provenance lives on each line-item value's source, and
            # a multi-year table legitimately spans several pages/reports. Aggregate those;
            # fall back to a table-level source if one happens to be present.
            srcs = [v.source for li in (getattr(t, "line_items", None) or [])
                    for v in (getattr(li, "values", None) or []) if getattr(v, "source", None)]
            if not srcs:
                tbl_src = getattr(t, "source", None)
                if tbl_src is not None:
                    srcs = [tbl_src]
            for src in srcs:
                _accumulate(acc, name, getattr(src, "report_file", None),
                            getattr(src, "pages", None), getattr(src, "table_id", None))
    return _finalize(acc)
