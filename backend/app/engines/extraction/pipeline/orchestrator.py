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

from app.core.config import get_settings
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
    production_ready: bool = True      # HEADLINE statements (P&L, BS) tie out to audited face truth
    fully_reconciled: bool = True      # STRICT: production_ready AND every detail row reconciles too
    validation_failures: int = 0       # production-blocking: headline-statement tie-out failures
    detail_incomplete: int = 0         # non-blocking for production_ready: breakdown / withheld leaves
    withheld: int = 0
    quarantined: int = 0
    manifest_path: Optional[str] = None
    # Sheet -> source-PDF provenance for a side-by-side viewer (see build_sheet_sources).
    sheet_sources: dict = {}


def process_documents(
    results: list[DocumentResult],
    output_path: str | Path,
    template_path: str | Path | None = None,
    company: str | None = None,
    dumper: DebugDumper | None = None,
    progress=None,
) -> ExtractionOutput:
    """Assemble per-report results into the final workbook."""
    def _emit(stage: str) -> None:
        if progress is None:
            return
        try:
            progress({"stage": stage})
        except Exception:  # noqa: BLE001
            pass

    dumper = dumper or DebugDumper(None)
    # Validation review (BETA) toggle: when off, the review ARTIFACTS are suppressed — the
    # 'Validation Ledger' sheet isn't written and suspicious cells aren't annotated, so the
    # frontend's review highlighting (which reads that sheet) goes inactive. The validation
    # is STILL computed (manifest counts, production gate) and corrections (formula plugs,
    # headline overrides) STILL apply — only the user-facing review surface is hidden.
    review = get_settings().validation_review_enabled
    _emit("merging")
    company_result = resolve_multiyear(results, company=company)
    output_path = str(output_path)

    dumper.subject(company_result.company or "company")
    dumper.json("04_multiyear", company_result)

    # Face truth is the most decision-dense layer (candidate selection, basis choice,
    # identity reconciliation) and what the headline override ships — so dump it with
    # provenance, plus a trace of every value-changing decision, to localize corruption.
    if dumper.enabled:
        from app.engines.extraction.services.face_truth import build_face_truth
        ft_trace: list = []
        ft = build_face_truth(company_result.tables, trace=ft_trace)
        dumper.json("06_face_truth", [
            {"metric": m, "year": y, "value": val,
             "source_table": getattr(src, "table_id", None),
             "pages": getattr(src, "pages", None),
             "title": getattr(src, "table_title", None)}
            for (m, y), (val, src) in sorted(ft.items())
        ])
        dumper.json("07_facetruth_decisions", ft_trace)

    if template_path:
        # Lazy import keeps openpyxl-template logic out of the no-template path.
        from app.engines.extraction.pipeline.template_map import apply_plan, build_plan

        _emit("mapping")
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
            identity_ledger, reconcile_breakdown_subtotals, template_ledger, write_source_ledger,
        )
        # Sign sensitivity: output statements (the formula/computed sheets) and any
        # cross-sheet pull are validated signed; intra-sheet breakdown subtotals by
        # magnitude. `formula_sheets` is the precise output-sheet set (not dividers).
        _emit("validating")
        output_set = set(plan.formula_sheets)
        computed_rows, computed_fail, computed_unevaluable = computed_output_ledger(
            output_path, company_result, _tieout, output_sheets=output_set, annotate=review)
        # Coverage gate: an emitted headline metric with NO face truth is unvalidated
        # (e.g. a mis-classified balance sheet yields no primary table) — block, don't
        # silently pass it just because there's nothing to compare against.
        coverage_rows, coverage_gaps = headline_coverage_gaps(
            output_path, company_result, output_set)
        # Accounting-identity consistency of face truth (advisory; catches face-truth
        # extraction errors the external tie-out can't, e.g. PAT != PBT - tax).
        identity_rows, identity_failures = identity_ledger(company_result)
        # Reconciliation-row gap-filling: make each breakdown subtotal tie to its audited
        # total, documenting the unmapped portion. Run AFTER detail_reconciliation is
        # captured from computed_rows (above) so that metric stays the genuine
        # 'leaves actually sum' number; this only makes the workbook subtotals correct.
        reconcile_rows, breakdown_reconciled = reconcile_breakdown_subtotals(
            output_path, company_result, _tieout, output_set, annotate=review)
        # Materiality-gated honesty: minor gaps (<=5%) are plugged (DETAIL_PLUG); material
        # ones are NOT (DETAIL_INCOMPLETE) — disclosed, not faked. Surface both + which
        # sheets carry material unmapped detail, so the workbook can't overclaim.
        detail_material_unmapped = sum(1 for r in reconcile_rows if r.status == "DETAIL_INCOMPLETE")
        detail_incomplete_sheets = sorted({r.sheet for r in reconcile_rows
                                           if r.status == "DETAIL_INCOMPLETE"})
        ledger_rows = template_ledger(plan) + computed_rows + coverage_rows + identity_rows + reconcile_rows
        if review:                                           # review off -> no ledger sheet
            append_ledger_sheet(output_path, ledger_rows)
        write_source_ledger(output_path, plan, overrides)    # traceability (#9) incl. output overrides
        if dumper.enabled:                                   # validation layer as JSON (greppable)
            from dataclasses import asdict, is_dataclass
            dumper.json("08_validation_ledger",
                        [asdict(r) if is_dataclass(r) else r for r in ledger_rows])
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
        # Objective detail-sheet accuracy: of the breakdown subtotals that resolve to a
        # face metric, how many reconcile to the audited total. Reported so progress on
        # detail mapping is measurable (codex acceptance test) and the manifest can't
        # imply the detail sheets are clean when they aren't.
        detail_rows = [r for r in computed_rows if r.sheet not in output_set]
        detail_checked = len(detail_rows)
        detail_ok = sum(1 for r in detail_rows if r.status == "ok")
        formulas_repaired = len(repairs)
        overrides_applied = len(overrides)
        # Sheet -> source-PDF page map (for a side-by-side PDF viewer): detail sheets
        # from plan.writes, headline output sheets from the audited overrides.
        from app.engines.extraction.services.provenance import build_sheet_sources
        sheet_sources = build_sheet_sources(mode="template", plan=plan, overrides=overrides)
    else:
        write_company_workbook(company_result, output_path)
        logger.info(
            "No-template workbook written: company=%r years=%s -> %s (%d tables)",
            company_result.company, company_result.fiscal_years, output_path, len(company_result.tables),
        )
        out = ExtractionOutput(output_path=output_path, company=company_result, mode="no_template")
        # P4 / C3: validate no-template key metrics against audited face truth.
        from app.engines.extraction.pipeline.template_map import _tieout
        from app.engines.extraction.services.validation import (
            append_ledger_sheet, identity_ledger, no_template_ledger,
        )
        ledger_rows, production_fail = no_template_ledger(company_result, _tieout)
        identity_rows, identity_failures = identity_ledger(company_result)
        if review:                                           # review off -> no ledger sheet
            append_ledger_sheet(output_path, ledger_rows + identity_rows)
        unevaluable = 0
        detail_incomplete = 0
        detail_checked = detail_ok = 0
        formulas_repaired = 0
        overrides_applied = 0
        coverage_gaps = 0
        breakdown_reconciled = 0
        detail_material_unmapped = 0
        detail_incomplete_sheets = []
        # Sheet -> source-PDF page map: one sheet per table, keyed by the same
        # sanitized sheet name the writer uses.
        from app.engines.extraction.services.provenance import build_sheet_sources
        sheet_sources = build_sheet_sources(mode="no_template", tables=company_result.tables)

    # Fit columns so large figures don't render as '######' (cosmetic, both modes).
    _emit("finalizing")
    from app.engines.extraction.services.validation import recalc_workbook, widen_columns
    widen_columns(output_path)
    # Embed the sheet -> source-PDF-page map INTO the workbook (custom doc property) so a
    # side-by-side viewer can sync the PDF page on sheet change straight from the file —
    # done BEFORE recalc so the subsequent save preserves it. (Also in the manifest below.)
    from app.engines.extraction.services.provenance import embed_sheet_sources
    embed_sheet_sources(output_path, sheet_sources)
    # Formula cache (#6): make formula cells readable by non-Excel consumers (sets
    # fullCalcOnLoad; materializes cached values too if LibreOffice is available).
    formula_cache_materialized = recalc_workbook(output_path)
    # Cash flow scope (#4): the template defines no cash-flow OUTPUT sheet, so cash flow is
    # out of the mapped deliverable. Declared explicitly so its absence isn't a silent gap;
    # CF data that WAS extracted still feeds the cash-flow identity checks in the ledger.
    output_titles = (out.plan.formula_sheets if out.plan else [])
    cash_flow_in_scope = any("cash" in s.lower() and "flow" in s.lower() for s in output_titles)
    # Self-describing scope note (#4): a first-tab sheet stating P&L/BS-only scope, cash-flow
    # status, and which detail schedules carry material unmapped detail — so the workbook
    # never reads as a fully-mapped three-statement deliverable when it isn't.
    if out.plan:
        from app.engines.extraction.services.validation import write_scope_note
        write_scope_note(output_path, cash_flow_in_scope=cash_flow_in_scope,
                         detail_incomplete_sheets=detail_incomplete_sheets)

    # Observability (#8): populate the result + write a manifest beside the workbook.
    # A run is production-ready only if every key metric tied out AND none was left
    # unvalidated (an un-evaluable key formula is a coverage gap, not a silent pass).
    out.production_ready = (production_fail == 0 and unevaluable == 0)
    # STRICT signal so the manifest never overclaims relative to the workbook ledger:
    # true only when EVERY ledger row reconciles — headline ties AND no breakdown
    # mismatch / withheld / unevaluable / coverage gap. `production_ready` certifies the
    # headline statements (the deliverable); `fully_reconciled` certifies the whole book.
    out.fully_reconciled = out.production_ready and detail_incomplete == 0 and identity_failures == 0
    out.validation_failures = production_fail
    out.detail_incomplete = detail_incomplete
    out.withheld = len(out.plan.withheld) if out.plan else 0
    out.quarantined = len(company_result.rejected_lines)
    out.sheet_sources = sheet_sources
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
        # Objective detail-sheet accuracy: breakdown subtotals GENUINELY reconciling to
        # audited totals (leaves actually sum), measured before any gap-filling.
        "detail_reconciliation": f"{detail_ok}/{detail_checked}" if detail_checked else "0/0",
        # Subtotals with a MINOR gap (<=5%) plugged so the schedule foots (DETAIL_PLUG).
        "breakdown_reconciled": breakdown_reconciled,
        # MATERIAL unmapped detail (>5% gap) — NOT plugged, disclosed honestly. This is the
        # real "weak detail" signal; subtotals here show their incomplete leaf-sum, not a
        # forced total. Plus the sheets that carry it.
        "detail_material_unmapped": detail_material_unmapped,
        "detail_incomplete_sheets": detail_incomplete_sheets,
        # Advisory: face-truth values failing an accounting identity (P&L waterfall / BS
        # composition) — flags suspect extraction the external tie-out can't catch.
        "identity_failures": identity_failures,
        # Production-blocking subset of validation_failures: headline metrics emitted with
        # no audited face truth (e.g. a mis-classified statement -> no primary table).
        "headline_coverage_gaps": coverage_gaps,
        "template_formulas_repaired": formulas_repaired,
        "headline_overrides": overrides_applied,
        # Delivery/coverage flags. cash_flow_in_scope=false means the template has no
        # cash-flow output sheet (CF is intentionally not part of the mapped deliverable).
        # formula_cache_materialized=false means formulas recalc on open in Excel but
        # cached values aren't populated for headless readers (no LibreOffice available).
        "cash_flow_in_scope": cash_flow_in_scope,
        "formula_cache_materialized": formula_cache_materialized,
        # Validation review (BETA): whether the review surface (Validation Ledger sheet +
        # in-cell suggestion annotations) was emitted. False => the workbook has no review
        # sheet, so the frontend's review highlighting stays inactive.
        "validation_review_enabled": review,
        # Per-sheet source provenance: {sheet -> [{report_file, pages, table_ids, weight}]},
        # ranked by contribution. Lets a side-by-side PDF viewer jump to the source page
        # when the user switches worksheets. Empty entries mean no page lineage survived.
        "sheet_sources": sheet_sources,
        # Production-ready iff every HEADLINE metric tied out and was evaluable.
        # See the 'Validation Ledger' / 'Source Ledger' sheets.
        "production_ready": out.production_ready,
        # Strict: true only when the ENTIRE ledger reconciles (no detail mismatch/withheld
        # either). When false but production_ready is true, the headline statements are
        # trustworthy and `detail_incomplete` says how many supporting rows don't reconcile.
        "fully_reconciled": out.fully_reconciled,
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
    progress=None,
) -> ExtractionOutput:
    """Full pipeline from PDFs to workbook (requires OPENAI_API_KEY for L3).

    `progress`, if given, is called with stage events {stage, pdf?, index?, total?} as the
    run advances — for live status APIs. It is best-effort and never affects the result."""
    # Imports here so the heavy/optional deps load only when actually extracting.
    from app.engines.extraction.pipeline.ingest import ingest_pdfs
    from app.engines.extraction.pipeline.interpret import interpret_document
    from app.engines.extraction.pipeline.tables import detect_tables
    from app.engines.extraction.services.gpt_client import GPTClient

    from datetime import datetime

    from app.core.debug import GPTRecorder, make_dumper
    from app.core.logging import per_document_log

    def _emit(stage: str, **kw) -> None:
        if progress is None:
            return
        try:
            progress({"stage": stage, **kw})
        except Exception:  # noqa: BLE001 — progress is best-effort, never break a run
            pass

    gpt = gpt or GPTClient()
    has_template = template_path is not None

    # One debug run dir for the whole company run (DEBUG only); GPT calls are
    # captured via a transparent recorder so every prompt/response is dumped.
    dumper = make_dumper(datetime.now().strftime("%Y%m%d_%H%M%S"))
    recording_gpt = GPTRecorder(gpt, dumper) if dumper.enabled else gpt

    from time import perf_counter

    results: list[DocumentResult] = []
    # Ingest/OCR is the bottleneck, so OCR every scanned page of every PDF through
    # one flat process pool up front (page- AND document-level parallelism). The
    # per-report detect+interpret stages then run serially on the ingested docs.
    n_pdfs = len(pdf_paths)
    _emit("ingesting", total=n_pdfs)
    t_ing = perf_counter()
    ingested = ingest_pdfs([Path(p) for p in pdf_paths])
    logger.info("Parallel ingest of %d PDF(s) completed in %.1fs", len(ingested), perf_counter() - t_ing)
    _emit("ingested", total=len(ingested))
    for idx, (pdf, doc) in enumerate(ingested, start=1):
        # Each PDF gets its own log file (logs/<timestamp>_<pdf>.log).
        with per_document_log(pdf.stem):
            logger.info("Processing report %s", pdf.name)
            dumper.subject(pdf.stem)
            # Per-report isolation: one bad PDF must not kill a multi-report run. Per-stage
            # timing makes each bottleneck measurable instead of inferred from log timestamps.
            try:
                dumper.json("01_ingest", doc)
                t1 = perf_counter()
                _emit("detecting_tables", pdf=pdf.name, index=idx, total=len(ingested))
                table_set = detect_tables(pdf, doc)
                dumper.json("02_tables", table_set)
                t2 = perf_counter()
                result = interpret_document(doc, table_set, recording_gpt,
                                            has_template=has_template, pdf_path=pdf,
                                            progress=progress, pdf_name=pdf.name)
                dumper.json("03_interpret", result)
                t3 = perf_counter()
                _emit("interpreted", pdf=pdf.name, index=idx, total=len(ingested))
                logger.info("Stage timings for %s: detect=%.1fs interpret=%.1fs (post-ingest total=%.1fs)",
                            pdf.name, t2 - t1, t3 - t2, t3 - t1)
                dumper.json("00_summary", _document_summary(doc, table_set, result))
                results.append(result)
            except Exception as exc:  # noqa: BLE001 — skip a failed report, keep the run alive
                logger.error("Skipping report %s — extraction failed: %s", pdf.name, exc, exc_info=True)

    if not results:
        raise RuntimeError("No reports could be processed (all failed); see logs.")
    return process_documents(
        results, output_path, template_path=template_path, company=company, dumper=dumper,
        progress=progress,
    )
