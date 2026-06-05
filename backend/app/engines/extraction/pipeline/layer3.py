"""Layer 3 orchestrator — structure tables + extract insights for one document."""
from __future__ import annotations

from app.core.logging import get_logger
from app.engines.extraction.models.common import TARGET_STATEMENT_TYPES
from app.engines.extraction.models.document import IngestedDoc
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.models.table import TableSet
from app.engines.extraction.pipeline.insights import extract_insights
from app.engines.extraction.pipeline.structure import structure_tables

logger = get_logger(__name__)


def run_layer3(
    doc: IngestedDoc,
    table_set: TableSet,
    gpt,
    has_template: bool = False,
) -> DocumentResult:
    """Run GPT structuring + insight extraction.

    Table emission policy:
      - has_template=True  -> only target tables (the template union) are kept;
        unclassified/other tables are rejected.
      - has_template=False -> ALL detected tables are emitted (one sheet each);
        unclassified/other tables become generic sheets in the output layer.

    Args:
        doc: Layer-1 IngestedDoc (narrative text source for insights).
        table_set: Layer-2 TableSet (detected/classified raw tables).
        gpt: a client exposing complete_structured(system, user, schema).
        has_template: whether a template was supplied for this run.
    """
    structured = structure_tables(table_set, gpt)

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
