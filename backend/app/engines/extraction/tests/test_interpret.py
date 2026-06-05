"""Interpretation-stage tests using a stub GPT client (no network / no API key)."""
from app.engines.extraction.models.classification import BatchClassification, TableClassification
from app.engines.extraction.models.common import SourceRef, StatementType
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.models.financials import (
    FinancialTable, FinancialTableList, LineItem, LineItemValue,
)
from app.engines.extraction.models.insight import INSIGHT_COLUMNS, Insight, InsightList
from app.engines.extraction.models.table import RawTable, TableSet
from app.engines.extraction.pipeline.insights import extract_insights
from app.engines.extraction.pipeline.structure import structure_tables
from app.engines.extraction.services import prompts


class FakeGPT:
    """Returns canned structured objects based on the requested schema."""

    def __init__(self, insights=None, batch=None, page_tables=None):
        self._insights = insights
        self._batch = batch
        self._page_tables = page_tables  # FinancialTableList for page extraction

    def complete_structured(self, system, user, schema):
        if schema is InsightList:
            return self._insights
        if schema is BatchClassification:
            return self._batch or BatchClassification()
        if schema is FinancialTableList:
            return self._page_tables or FinancialTableList()
        raise AssertionError(f"unexpected schema {schema}")


def _raw_table(**kw) -> RawTable:
    base = dict(
        table_id="report.pdf::t0",
        statement_type=StatementType.income_statement,
        title="Income Statement",
        header=["", "2024", "2025"],
        rows=[["Revenue", "100", "120"]],
        years=[2024, 2025],
        needs_review=False,
        source=SourceRef(report_file="report.pdf", report_year=2025, pages=[5], section="Income Statement"),
    )
    base.update(kw)
    return RawTable(**base)


def test_structure_is_rule_based_and_parses_values():
    raw = _raw_table(rows=[["Revenue", "1,000", "(50)"], ["", "", ""]])
    out = structure_tables(TableSet(file_name="report.pdf", tables=[raw]))  # no GPT needed
    assert len(out) == 1
    ft = out[0]
    assert ft.statement_type == StatementType.income_statement
    assert ft.source.pages == [5] and ft.years == [2024, 2025]
    rev = ft.line_items[0]
    vals = {v.year: v.value for v in rev.values}
    assert vals[2024] == 1000.0 and vals[2025] == -50.0  # parens -> negative


def test_no_gpt_request_when_all_classified():
    class Boom:
        def complete_structured(self, *a, **k):
            raise AssertionError("GPT must not be called when all tables are classified")

    ts = TableSet(file_name="report.pdf", tables=[_raw_table()])  # needs_review=False
    out = structure_tables(ts, Boom())
    assert out[0].statement_type == StatementType.income_statement


def test_unclassified_grid_classified_and_reconstructed_by_gpt():
    # Unclassified grid -> GPT classifies AND reconstructs it in one call.
    canned = FinancialTableList(tables=[FinancialTable(
        statement_type=StatementType.finance_cost, title="Finance Cost",
        line_items=[LineItem(label="Bank charges", values=[LineItemValue(year=2025, value=42.0)])],
    )])
    ts = TableSet(file_name="report.pdf", tables=[
        _raw_table(table_id="report.pdf::t0", needs_review=True, statement_type=StatementType.unclassified),
    ])
    out = structure_tables(ts, FakeGPT(page_tables=canned))
    assert len(out) == 1
    assert out[0].statement_type == StatementType.finance_cost          # GPT classified
    assert out[0].line_items[0].values[0].value == 42.0                 # GPT reconstructed
    assert out[0].source.pages == [5]                                    # provenance from the grid


def test_unclassified_grid_kept_rule_based_when_not_financial():
    # GPT returns no table -> keep the rule-based grid as `unclassified`
    # (so no-template still emits it; the template path filters it out later).
    ts = TableSet(file_name="report.pdf", tables=[
        _raw_table(needs_review=True, statement_type=StatementType.unclassified),
    ])
    out = structure_tables(ts, FakeGPT(page_tables=FinancialTableList()))
    assert len(out) == 1 and out[0].statement_type == StatementType.unclassified


def test_gpt_grid_failure_is_graceful():
    class Boom:
        def complete_structured(self, *a, **k):
            raise RuntimeError("api down")

    ts = TableSet(file_name="report.pdf", tables=[_raw_table(needs_review=True, statement_type=StatementType.unclassified)])
    out = structure_tables(ts, Boom())
    # GPT failed -> fall back to rule-based grid, kept as unclassified (no crash).
    assert len(out) == 1 and out[0].statement_type == StatementType.unclassified


def test_interpret_template_rejects_non_target():
    from app.engines.extraction.pipeline.interpret import interpret_document

    doc = IngestedDoc(file_name="report.pdf", report_year=2025)
    doc.pages = [PageText(page=1, text="Outlook for the business.", kind=PageKind.native, char_count=25)]
    doc.page_count = 1
    ts = TableSet(file_name="report.pdf", tables=[_raw_table(needs_review=True, statement_type=StatementType.unclassified)])
    gpt = FakeGPT(insights=InsightList(), batch=BatchClassification())

    # With a template: unclassified/other tables are rejected.
    result = interpret_document(doc, ts, gpt, has_template=True)
    assert result.tables == []


def test_interpret_no_template_keeps_all_tables():
    from app.engines.extraction.pipeline.interpret import interpret_document

    doc = IngestedDoc(file_name="report.pdf", report_year=2025)
    doc.pages = [PageText(page=1, text="Outlook for the business.", kind=PageKind.native, char_count=25)]
    doc.page_count = 1
    ts = TableSet(file_name="report.pdf", tables=[_raw_table(needs_review=True, statement_type=StatementType.unclassified)])
    gpt = FakeGPT(insights=InsightList(), batch=BatchClassification())

    # No template: every detected table is emitted (one sheet each downstream).
    result = interpret_document(doc, ts, gpt, has_template=False)
    assert len(result.tables) == 1


_FIN_PAGE = (
    "Statement of Profit or Loss\n(Rupees in thousand)\nNote 2025 2024\n"
    "Revenue from contracts with customers  53,347,603  95,020,571\n"
    "Cost of sales  (38,940,489)  (71,048,945)\n"
    "Gross profit  14,407,114  23,971,626\n"
    "Operating expenses  (4,703,226)  (5,359,314)\n"
)


def test_gpt_extracts_financial_tables_from_page_text():
    from app.engines.extraction.pipeline.gpt_tables import extract_financial_tables

    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [PageText(page=190, text=_FIN_PAGE, kind=PageKind.ocr, char_count=len(_FIN_PAGE))]
    doc.page_count = 1
    canned = FinancialTableList(tables=[FinancialTable(
        statement_type=StatementType.income_statement, title="P&L",
        line_items=[LineItem(label="Revenue from contracts with customers",
                             values=[LineItemValue(year=2025, value=53347603.0)])],
    )])
    out = extract_financial_tables(doc, FakeGPT(page_tables=canned))
    assert len(out) == 1
    t = out[0]
    assert t.source.pages == [190] and t.years == [2025]
    # canonical metric resolved locally
    assert t.line_items[0].canonical_metric == "revenue"


def test_parallel_extraction_preserves_page_order():
    from app.engines.extraction.pipeline.gpt_tables import extract_financial_tables

    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [PageText(page=p, text=_FIN_PAGE, kind=PageKind.native, char_count=len(_FIN_PAGE))
                 for p in (100, 101, 102)]
    doc.page_count = 3

    class PerPageGPT:  # fresh object per call (thread-safe, like the real client)
        def complete_structured(self, system, user, schema):
            return FinancialTableList(tables=[FinancialTable(
                statement_type=StatementType.income_statement,
                line_items=[LineItem(label="Revenue", values=[LineItemValue(year=2025, value=1.0)])])])

    out = extract_financial_tables(doc, PerPageGPT())   # runs concurrently (workers=3)
    # All pages processed; output ordered by page regardless of completion order.
    assert [t.source.pages[0] for t in out] == [100, 101, 102]


def test_parallel_extraction_stable_when_pages_finish_out_of_order():
    import re
    import time
    from app.engines.extraction.pipeline.gpt_tables import extract_financial_tables

    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [PageText(page=p, text=_FIN_PAGE, kind=PageKind.native, char_count=len(_FIN_PAGE))
                 for p in (100, 101, 102)]
    doc.page_count = 3

    class OutOfOrderGPT:  # earlier pages return LATER -> forces out-of-order completion
        def complete_structured(self, system, user, schema):
            page = int(re.search(r"Page:\s*(\d+)", user).group(1))
            time.sleep((102 - page) * 0.02)   # p102 finishes first, p100 last
            return FinancialTableList(tables=[FinancialTable(
                statement_type=StatementType.income_statement,
                line_items=[LineItem(label="Revenue", values=[LineItemValue(year=2025, value=1.0)])])])

    out = extract_financial_tables(doc, OutOfOrderGPT())
    assert [t.source.pages[0] for t in out] == [100, 101, 102]  # sorted, not completion order


def test_interpret_prefers_gpt_over_rule_for_same_type():
    from app.engines.extraction.pipeline.interpret import interpret_document

    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [PageText(page=190, text=_FIN_PAGE, kind=PageKind.ocr, char_count=len(_FIN_PAGE))]
    doc.page_count = 1
    # A rule-based (native) income_statement table also exists; GPT must win.
    ts = TableSet(file_name="r.pdf", tables=[_raw_table()])  # native income_statement
    canned = FinancialTableList(tables=[FinancialTable(
        statement_type=StatementType.income_statement, title="P&L",
        line_items=[LineItem(label="Revenue", values=[LineItemValue(year=2025, value=999.0)])],
    )])
    gpt = FakeGPT(insights=InsightList(), batch=BatchClassification(), page_tables=canned)
    result = interpret_document(doc, ts, gpt, has_template=False)
    is_tables = [t for t in result.tables if t.statement_type == StatementType.income_statement]
    assert len(is_tables) == 1                      # GPT's, not the rule-based one too
    assert is_tables[0].line_items[0].values[0].value == 999.0


def test_interpret_ocr_grids_not_used_when_gpt_on():
    from app.engines.extraction.pipeline.interpret import interpret_document

    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [PageText(page=1, text="Outlook for the business.", kind=PageKind.native, char_count=25)]
    doc.page_count = 1
    # A garbled OCR-sourced table must be ignored by the rule-based path.
    ts = TableSet(file_name="r.pdf", tables=[_raw_table(from_ocr=True, statement_type=StatementType.income_statement)])
    gpt = FakeGPT(insights=InsightList(), batch=BatchClassification())
    result = interpret_document(doc, ts, gpt, has_template=False)
    assert result.tables == []  # OCR grid dropped, GPT produced nothing for this doc


_NARRATIVE = (
    "Export volumes surged 53 percent year on year supported by strong global "
    "demand, and the company expanded capacity while managing working capital and "
    "debt prudently across its export markets and margins."
)


def _doc() -> IngestedDoc:
    doc = IngestedDoc(file_name="report.pdf", report_year=2025)
    doc.pages = [PageText(page=10, text=f"CEO Review\n{_NARRATIVE}", kind=PageKind.native, char_count=200)]
    doc.page_count = 1
    return doc


def test_extract_insights_provenance_and_export():
    canned = InsightList(insights=[
        Insight(area="Export growth", takeaway="Export volumes surged 53%.",
                source_section="CEO Review", page=10, confidence=0.98),
    ])
    exported, review = extract_insights(_doc(), FakeGPT(insights=canned))
    assert len(exported) == 1 and not review
    ins = exported[0]
    assert ins.source_report_year == 2025 and ins.year == 2025 and ins.page == 10


def test_extract_insights_rejects_bad_provenance():
    # Cites a page we never sent -> dropped.
    canned = InsightList(insights=[
        Insight(area="Export growth", takeaway="x", source_section="CEO Review", page=999, confidence=0.98),
    ])
    exported, review = extract_insights(_doc(), FakeGPT(insights=canned))
    assert not exported and not review


def test_extract_insights_review_bucket():
    canned = InsightList(insights=[
        Insight(area="Margins", takeaway="Margins were mixed.",
                source_section="CEO Review", page=10, confidence=0.6),
    ])
    exported, review = extract_insights(_doc(), FakeGPT(insights=canned))
    assert not exported and len(review) == 1


def test_insight_row_matches_columns():
    ins = Insight(
        year=2025, source_report_year=2025, area="Foreign operations",
        takeaway="Operations remained profitable.", source_section="CEO Review",
        page=10, confidence=0.95,
    )
    row = ins.to_row()
    assert len(row) == len(INSIGHT_COLUMNS)
    assert row[0] == 2025 and row[2] == "Foreign operations" and row[-1] == 0.95


def test_classify_prompt_renders():
    s, u = prompts.render("classify", allowed_types="revenue, cost_of_sales", tables="- t0 :: x")
    assert '"classifications"' in u and s
