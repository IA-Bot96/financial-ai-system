"""Layer 3 tests using a stub GPT client (no network / no API key)."""
from app.engines.extraction.models.classification import BatchClassification, TableClassification
from app.engines.extraction.models.common import SourceRef, StatementType
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.models.financials import FinancialTable, LineItem, LineItemValue
from app.engines.extraction.models.insight import INSIGHT_COLUMNS, Insight, InsightList
from app.engines.extraction.models.table import RawTable, TableSet
from app.engines.extraction.pipeline.insights import extract_insights
from app.engines.extraction.pipeline.structure import structure_tables
from app.engines.extraction.services import prompts


class FakeGPT:
    """Returns canned structured objects based on the requested schema."""

    def __init__(self, insights=None, batch=None):
        self._insights = insights
        self._batch = batch

    def complete_structured(self, system, user, schema):
        if schema is InsightList:
            return self._insights
        if schema is BatchClassification:
            return self._batch or BatchClassification()
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


def test_ambiguous_tables_classified_in_one_batch():
    calls = {"n": 0}
    batch = BatchClassification(classifications=[
        TableClassification(table_id="report.pdf::t0", statement_type=StatementType.finance_cost),
        TableClassification(table_id="report.pdf::t1", statement_type=StatementType.revenue),
    ])

    class CountingGPT(FakeGPT):
        def complete_structured(self, system, user, schema):
            calls["n"] += 1
            return super().complete_structured(system, user, schema)

    ts = TableSet(file_name="report.pdf", tables=[
        _raw_table(table_id="report.pdf::t0", needs_review=True, statement_type=StatementType.unclassified),
        _raw_table(table_id="report.pdf::t1", needs_review=True, statement_type=StatementType.unclassified),
    ])
    out = structure_tables(ts, CountingGPT(batch=batch))
    assert calls["n"] == 1  # ONE request for both ambiguous tables
    assert out[0].statement_type == StatementType.finance_cost
    assert out[1].statement_type == StatementType.revenue


def test_batch_classify_failure_is_graceful():
    class Boom:
        def complete_structured(self, *a, **k):
            raise RuntimeError("api down")

    ts = TableSet(file_name="report.pdf", tables=[_raw_table(needs_review=True, statement_type=StatementType.unclassified)])
    out = structure_tables(ts, Boom())
    assert out[0].statement_type == StatementType.unclassified  # unchanged, no crash


def test_run_layer3_template_rejects_non_target():
    from app.engines.extraction.pipeline.layer3 import run_layer3

    doc = IngestedDoc(file_name="report.pdf", report_year=2025)
    doc.pages = [PageText(page=1, text="Outlook for the business.", kind=PageKind.native, char_count=25)]
    doc.page_count = 1
    ts = TableSet(file_name="report.pdf", tables=[_raw_table(needs_review=True, statement_type=StatementType.unclassified)])
    gpt = FakeGPT(insights=InsightList(), batch=BatchClassification())

    # With a template: unclassified/other tables are rejected.
    result = run_layer3(doc, ts, gpt, has_template=True)
    assert result.tables == []


def test_run_layer3_no_template_keeps_all_tables():
    from app.engines.extraction.pipeline.layer3 import run_layer3

    doc = IngestedDoc(file_name="report.pdf", report_year=2025)
    doc.pages = [PageText(page=1, text="Outlook for the business.", kind=PageKind.native, char_count=25)]
    doc.page_count = 1
    ts = TableSet(file_name="report.pdf", tables=[_raw_table(needs_review=True, statement_type=StatementType.unclassified)])
    gpt = FakeGPT(insights=InsightList(), batch=BatchClassification())

    # No template: every detected table is emitted (one sheet each downstream).
    result = run_layer3(doc, ts, gpt, has_template=False)
    assert len(result.tables) == 1


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
