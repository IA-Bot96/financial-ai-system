"""Interpretation stage — turn detected tables + narrative into a structured
DocumentResult (GPT structuring of tables, ambiguous-table classification, and
insight extraction) for one document.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.common import TARGET_STATEMENT_TYPES
from app.engines.extraction.models.document import IngestedDoc
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.models.table import TableSet
from app.engines.extraction.pipeline.gpt_tables import extract_financial_tables
from app.engines.extraction.pipeline.insights import extract_insights
from app.engines.extraction.pipeline.structure import structure_tables

logger = get_logger(__name__)


def interpret_document(
    doc: IngestedDoc,
    table_set: TableSet,
    gpt,
    has_template: bool = False,
    pdf_path=None,
) -> DocumentResult:
    """Structure tables + extract insights for one document.

    Table emission policy:
      - has_template=True  -> only target tables (the template union) are kept;
        unclassified/other tables are rejected.
      - has_template=False -> ALL detected tables are emitted (one sheet each);
        unclassified/other tables become generic sheets in the output layer.

    Args:
        doc: ingested document (narrative text source for insights).
        table_set: detected/classified raw tables.
        gpt: a client exposing complete_structured(system, user, schema).
        has_template: whether a template was supplied for this run.
    """
    settings = get_settings()

    # GPT-extract financial statements/notes from page text — robust on scanned
    # pages where rule-based grid reconstruction fails.
    gpt_tables = []
    if gpt is not None and settings.use_gpt_table_extraction:
        # Template targets unconsolidated -> skip the consolidated set to halve calls.
        gpt_tables = extract_financial_tables(
            doc, gpt, skip_consolidated=has_template, pdf_path=pdf_path)
    gpt_types = {t.statement_type for t in gpt_tables}

    # Rule-based path only for NATIVE tables (scanned/OCR grids are unreliable —
    # those pages are covered by GPT above). Drop rule-based tables of a type GPT
    # already produced, so GPT wins for financial statements.
    native_set = table_set.model_copy(update={
        "tables": [t for t in table_set.tables if not t.from_ocr],
    })
    rule_tables = [t for t in structure_tables(native_set, gpt) if t.statement_type not in gpt_types]

    structured = gpt_tables + rule_tables

    if has_template:
        tables = [t for t in structured if t.statement_type in TARGET_STATEMENT_TYPES]
        rejected = len(structured) - len(tables)
        if rejected:
            logger.info("Rejected %d non-target table(s) from %s", rejected, doc.file_name)
    else:
        tables = structured  # no template -> emit every detected table

    exported, review = extract_insights(doc, gpt)
    return DocumentResult(
        file_name=doc.file_name,
        company=doc.company,
        report_year=doc.report_year,
        tables=tables,
        insights=exported,
        insights_review=review,
    )
