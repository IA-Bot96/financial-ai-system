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

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from app.core.debug import DebugDumper
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


def _document_summary(doc, table_set, result) -> dict:
    from collections import Counter
    return {
        "file_name": doc.file_name,
        "company": doc.company,
        "report_year": doc.report_year,
        "pages": doc.page_count,
        "ocr_pages": doc.ocr_page_count,
        "is_scanned": doc.is_scanned,
        "grids_detected": len(table_set.tables),
        "tables_emitted": len(result.tables),
        "tables_by_type": dict(Counter(t.statement_type.value for t in result.tables)),
        "insights": len(result.insights),
        "insights_review": len(result.insights_review),
    }


class ExtractionOutput(BaseModel):
    output_path: str
    company: CompanyResult
    mode: str                      # "template" | "no_template"
    plan: Optional[MappingPlan] = None
    # Observability (#8): validation outcome surfaced to callers / API / manifest.
    production_ready: bool = True
    validation_failures: int = 0       # production-blocking: headline-statement tie-out failures
    detail_incomplete: int = 0         # non-blocking: breakdown subtotals / withheld leaves
    withheld: int = 0
    quarantined: int = 0
    manifest_path: Optional[str] = None


def process_documents(
    results: list[DocumentResult],
    output_path: str | Path,
    template_path: str | Path | None = None,
    company: str | None = None,
    dumper: DebugDumper | None = None,
) -> ExtractionOutput:
    """Assemble per-report results into the final workbook."""
    dumper = dumper or DebugDumper(None)
    company_result = resolve_multiyear(results, company=company)
    output_path = str(output_path)

    dumper.subject(company_result.company or "company")
    dumper.json("04_multiyear", company_result)

    if template_path:
        # Lazy import keeps openpyxl-template logic out of the no-template path.
        from app.engines.extraction.pipeline.template_map import apply_plan, build_plan

        plan = build_plan(company_result, template_path)
        dumper.json("05_mapping_plan", plan)
        apply_plan(plan, template_path, output_path, company=company_result.company)
        # Bucket A — repair template-author formula defects (frozen $col$row refs that
        # don't shift across year columns; literal-0 check cells that mask imbalances)
        # BEFORE validation, so the computed tie-out reflects the repaired formulas.
        from app.engines.extraction.services.formula_repair import repair_template_formulas
        repairs = repair_template_formulas(output_path)
        if repairs:
            logger.info("Template formula repair: fixed %d cell(s): %s",
                        len(repairs), "; ".join(str(r) for r in repairs[:8]))
        # Bucket B — substitute audited face truth into output-sheet headline cells that
        # don't tie out (incomplete/mis-placed breakdown leaves), so the delivered
        # statements are correct. Breakdown notes are left flagged, not faked.
        from app.engines.extraction.pipeline.template_map import _tieout
        from app.engines.extraction.services.headline_override import override_headline_metrics
        overrides = override_headline_metrics(
            output_path, company_result, _tieout, set(plan.formula_sheets))
        if overrides:
            logger.info("Headline override: substituted audited face truth into %d output cell(s): %s",
                        len(overrides), "; ".join(str(o) for o in overrides[:8]))
        append_insights_sheets(output_path, company_result.insights, company_result.insights_review)
        logger.info(
            "Template workbook written: company=%r years=%s -> %s (%d writes)",
            company_result.company, company_result.fiscal_years, output_path, len(plan.writes),
        )
        out = ExtractionOutput(output_path=output_path, company=company_result, mode="template", plan=plan)
        # P4: audit ledger + non-production flag. Combines (a) leaf-level withholds
        # from the plan and (b) the #1 computed-formula tie-out, which evaluates the
        # written workbook's output/subtotal formulas against the audited face truth.
        from app.engines.extraction.pipeline.template_map import _tieout
        from app.engines.extraction.services.validation import (
            append_ledger_sheet, computed_output_ledger, headline_coverage_gaps,
            template_ledger, write_source_ledger,
        )
        # Sign sensitivity: output statements (the formula/computed sheets) and any
        # cross-sheet pull are validated signed; intra-sheet breakdown subtotals by
        # magnitude. `formula_sheets` is the precise output-sheet set (not dividers).
        output_set = set(plan.formula_sheets)
        computed_rows, computed_fail, computed_unevaluable = computed_output_ledger(
            output_path, company_result, _tieout, output_sheets=output_set)
        # Coverage gate: an emitted headline metric with NO face truth is unvalidated
        # (e.g. a mis-classified balance sheet yields no primary table) — block, don't
        # silently pass it just because there's nothing to compare against.
        coverage_rows, coverage_gaps = headline_coverage_gaps(
            output_path, company_result, output_set)
        ledger_rows = template_ledger(plan) + computed_rows + coverage_rows
        append_ledger_sheet(output_path, ledger_rows)
        write_source_ledger(output_path, plan)               # traceability (#9)
        # Production gate certifies the HEADLINE statements (output sheets): every key
        # metric there must tie out to audited face truth, be evaluable, AND have face
        # truth to validate against. Breakdown-note gaps (incomplete leaves) + withheld
        # leaves are surfaced as non-blocking "detail incomplete".
        headline_fail = sum(1 for r in computed_rows
                            if r.status == "MISMATCH" and r.sheet in output_set)
        headline_uneval = sum(1 for r in computed_rows
                              if r.status == "UNEVALUATED" and r.sheet in output_set)
        production_fail = headline_fail + coverage_gaps
        unevaluable = headline_uneval
        detail_incomplete = (computed_fail - headline_fail) \
            + (computed_unevaluable - headline_uneval) + len(plan.withheld)
        formulas_repaired = len(repairs)
        overrides_applied = len(overrides)
    else:
        write_company_workbook(company_result, output_path)
        logger.info(
            "No-template workbook written: company=%r years=%s -> %s (%d tables)",
            company_result.company, company_result.fiscal_years, output_path, len(company_result.tables),
        )
        out = ExtractionOutput(output_path=output_path, company=company_result, mode="no_template")
        # P4 / C3: validate no-template key metrics against audited face truth.
        from app.engines.extraction.pipeline.template_map import _tieout
        from app.engines.extraction.services.validation import append_ledger_sheet, no_template_ledger
        ledger_rows, production_fail = no_template_ledger(company_result, _tieout)
        append_ledger_sheet(output_path, ledger_rows)
        unevaluable = 0
        detail_incomplete = 0
        formulas_repaired = 0
        overrides_applied = 0
        coverage_gaps = 0

    # Observability (#8): populate the result + write a manifest beside the workbook.
    # A run is production-ready only if every key metric tied out AND none was left
    # unvalidated (an un-evaluable key formula is a coverage gap, not a silent pass).
    out.production_ready = (production_fail == 0 and unevaluable == 0)
    out.validation_failures = production_fail
    out.detail_incomplete = detail_incomplete
    out.withheld = len(out.plan.withheld) if out.plan else 0
    out.quarantined = len(company_result.rejected_lines)
    manifest = {
        "company": company_result.company, "mode": out.mode,
        "fiscal_years": company_result.fiscal_years, "output_path": out.output_path,
        "source_reports": company_result.source_reports, "tables": len(company_result.tables),
        "writes": len(out.plan.writes) if out.plan else None,
        "unmatched_template_labels": len(out.plan.unmatched_template_labels) if out.plan else None,
        "withheld": out.withheld,
        "quarantined_lines": out.quarantined,
        # Production-blocking: headline-statement (output-sheet) tie-out failures.
        "validation_failures": production_fail,
        "unevaluable_formulas": unevaluable,
        # Non-blocking: breakdown-note gaps + withheld leaves (flagged, not shipped silently).
        "detail_incomplete": detail_incomplete,
        # Production-blocking subset of validation_failures: headline metrics emitted with
        # no audited face truth (e.g. a mis-classified statement -> no primary table).
        "headline_coverage_gaps": coverage_gaps,
        "template_formulas_repaired": formulas_repaired,
        "headline_overrides": overrides_applied,
        # Production-ready iff every HEADLINE metric tied out and was evaluable.
        # See the 'Validation Ledger' / 'Source Ledger' sheets.
        "production_ready": out.production_ready,
    }
    dumper.json("00_run_summary", manifest)
    try:
        manifest_path = Path(output_path).with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        out.manifest_path = str(manifest_path)
    except Exception as exc:  # noqa: BLE001 — manifest is best-effort, never fail the run
        logger.warning("Could not write manifest: %s", exc)
    if production_fail:
        logger.warning("Run is NON-PRODUCTION: %d headline value(s) failed validation "
                       "(%d tie-out mismatch, %d with no audited face truth) — see 'Validation Ledger'",
                       production_fail, production_fail - coverage_gaps, coverage_gaps)
    elif detail_incomplete:
        logger.info("Headline statements tie out; %d breakdown-detail row(s) remain incomplete "
                    "(non-blocking, see 'Validation Ledger' sheet)", detail_incomplete)
    return out


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
    from app.engines.extraction.pipeline.interpret import interpret_document
    from app.engines.extraction.pipeline.tables import detect_tables
    from app.engines.extraction.services.gpt_client import GPTClient

    from datetime import datetime

    from app.core.debug import GPTRecorder, make_dumper
    from app.core.logging import per_document_log

    gpt = gpt or GPTClient()
    has_template = template_path is not None

    # One debug run dir for the whole company run (DEBUG only); GPT calls are
    # captured via a transparent recorder so every prompt/response is dumped.
    dumper = make_dumper(datetime.now().strftime("%Y%m%d_%H%M%S"))
    recording_gpt = GPTRecorder(gpt, dumper) if dumper.enabled else gpt

    results: list[DocumentResult] = []
    for pdf in pdf_paths:
        pdf = Path(pdf)
        # Each PDF gets its own log file (logs/<timestamp>_<pdf>.log).
        with per_document_log(pdf.stem):
            logger.info("Processing report %s", pdf.name)
            dumper.subject(pdf.stem)
            doc = ingest_pdf(pdf)
            dumper.json("01_ingest", doc)
            table_set = detect_tables(pdf, doc)
            dumper.json("02_tables", table_set)
            result = interpret_document(doc, table_set, recording_gpt, has_template=has_template)
            dumper.json("03_interpret", result)
            dumper.json("00_summary", _document_summary(doc, table_set, result))
            results.append(result)

    return process_documents(
        results, output_path, template_path=template_path, company=company, dumper=dumper,
    )
