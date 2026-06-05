"""Layer 7 — cross-report orchestration.

Runs the full pipeline for one company's set of reports:
  per PDF: ingest (L1) -> detect tables (L2) -> structure + insights (L3)
  then:    multi-year resolve (L4)
  then:    template-fill (L5/L6) OR no-template workbook (L6)

`process_documents` (assembly + output) is separated from `process_reports`
(which also runs the per-PDF extraction) so the output routing is testable
without real PDFs or a live GPT.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from app.core.logging import get_logger
from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.mapping import MappingPlan
from app.engines.extraction.models.result import DocumentResult
from app.engines.extraction.pipeline.excel_writer import (
    append_insights_sheets,
    write_company_workbook,
)
from app.engines.extraction.pipeline.multiyear import resolve_multiyear

logger = get_logger(__name__)


class ExtractionOutput(BaseModel):
    output_path: str
    company: CompanyResult
    mode: str                      # "template" | "no_template"
    plan: Optional[MappingPlan] = None


def process_documents(
    results: list[DocumentResult],
    output_path: str | Path,
    template_path: str | Path | None = None,
    company: str | None = None,
) -> ExtractionOutput:
    """Assemble per-report results into the final workbook."""
    company_result = resolve_multiyear(results, company=company)
    output_path = str(output_path)

    if template_path:
        # Lazy import keeps openpyxl-template logic out of the no-template path.
        from app.engines.extraction.pipeline.template_map import apply_plan, build_plan

        plan = build_plan(company_result, template_path)
        apply_plan(plan, template_path, output_path)
        append_insights_sheets(output_path, company_result.insights, company_result.insights_review)
        logger.info(
            "Template workbook written: company=%r years=%s -> %s (%d writes)",
            company_result.company, company_result.fiscal_years, output_path, len(plan.writes),
        )
        return ExtractionOutput(output_path=output_path, company=company_result, mode="template", plan=plan)

    write_company_workbook(company_result, output_path)
    logger.info(
        "No-template workbook written: company=%r years=%s -> %s (%d tables)",
        company_result.company, company_result.fiscal_years, output_path, len(company_result.tables),
    )
    return ExtractionOutput(output_path=output_path, company=company_result, mode="no_template")


def process_reports(
    pdf_paths: list[str | Path],
    output_path: str | Path,
    template_path: str | Path | None = None,
    company: str | None = None,
    gpt=None,
) -> ExtractionOutput:
    """Full pipeline from PDFs to workbook (requires OPENAI_API_KEY for L3)."""
    # Imports here so the heavy/optional deps load only when actually extracting.
    from app.engines.extraction.pipeline.ingest import ingest_pdf
    from app.engines.extraction.pipeline.layer3 import run_layer3
    from app.engines.extraction.pipeline.tables import detect_tables
    from app.engines.extraction.services.gpt_client import GPTClient

    gpt = gpt or GPTClient()
    has_template = template_path is not None

    from app.core.logging import per_document_log

    results: list[DocumentResult] = []
    for pdf in pdf_paths:
        pdf = Path(pdf)
        # Each PDF gets its own log file (logs/<timestamp>_<pdf>.log).
        with per_document_log(pdf.stem):
            logger.info("Processing report %s", pdf.name)
            doc = ingest_pdf(pdf)
            table_set = detect_tables(pdf, doc)
            results.append(run_layer3(doc, table_set, gpt, has_template=has_template))

    return process_documents(results, output_path, template_path=template_path, company=company)
