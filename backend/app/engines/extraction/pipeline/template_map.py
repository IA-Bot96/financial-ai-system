"""Layer 5 — Template mapping (rule-based + label matching).

Populate a template's breakdown input sheets from a multi-year CompanyResult:

  - Detect each sheet's year-header row and which columns are HISTORICAL vs
    FORECAST (forecast columns are never written).
  - Skip computed/output sheets (mostly formulas -> low empty fraction) and
    non-data sheets (no year header).
  - For each leaf input row (mixed-case label, writable/empty value cells), match
    the label to an extracted line item (scoped to the sheet's statement type via
    the classifier) and write the value for each historical year — only into
    empty, non-formula cells, so subtotals/formulas are preserved.

`build_plan` is pure (reads the template, returns a MappingPlan). `apply_plan`
writes the plan into a copy of the template and saves it.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.company import CompanyResult
from app.engines.extraction.models.mapping import CellWrite, MappingPlan
from app.engines.extraction.services.classifier import TableClassifier
from app.engines.extraction.services.metric_resolver import spaced

logger = get_logger(__name__)

_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# Generic section words shared across sections (don't distinguish Local vs Export).
_SECTION_STOPWORDS = {
    "sales", "cost", "costs", "expense", "expenses", "income", "revenue",
    "total", "net", "gross", "and", "the", "of", "to", "from", "other",
}


def _significant_tokens(section_norm: str) -> set[str]:
    return {t for t in section_norm.split() if len(t) >= 3 and t not in _SECTION_STOPWORDS}


def _section_compatible(template_section_norm: str, cand_section_norm: str) -> bool:
    """Sections are compatible unless both carry distinctive tokens that don't
    overlap — so 'EXPORT SALES' (export) is rejected against 'LOCAL SALES' (local),
    but a missing/uninformative section never blocks a match."""
    t = _significant_tokens(template_section_norm)
    c = _significant_tokens(cand_section_norm)
    if not t or not c:
        return True
    return bool(t & c)


# --- cell helpers ---

def _is_formula(cell) -> bool:
    if getattr(cell, "data_type", None) == "f":
        return True
    v = cell.value
    return isinstance(v, str) and v.startswith("=")


def _is_empty(cell) -> bool:
    return cell.value is None or (isinstance(cell.value, str) and not cell.value.strip())


def _as_year(value) -> int | None:
    if isinstance(value, (int, float)) and 1990 <= int(value) <= 2100:
        return int(value)
    if isinstance(value, str):
        m = _YEAR_RE.search(value)
        if m:
            return int(m.group())
    return None


def _is_section_header(label: str) -> bool:
    letters = [c for c in label if c.isalpha()]
    if len(letters) < 3:
        return True
    return sum(c.isupper() for c in letters) / len(letters) >= 0.7


# --- sheet structure detection ---

def _detect_year_columns(ws) -> tuple[dict[int, int], int, int | None]:
    """Return (historical {col->year}, header_row, forecast_start_col).

    Forecast boundary is taken from a 'forecast' marker cell if present; columns
    at/after it are excluded from the historical map.
    """
    best: dict[int, int] = {}
    header_row = 1
    scan = min(ws.max_row, 8)
    for r in range(1, scan + 1):
        cols = {c: y for c in range(1, ws.max_column + 1)
                if (y := _as_year(ws.cell(r, c).value)) is not None}
        if len(cols) > len(best):
            best, header_row = cols, r

    forecast_start: int | None = None
    for r in range(1, scan + 1):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and "forecast" in v.lower():
                forecast_start = c if forecast_start is None else min(forecast_start, c)
    historical = {c: y for c, y in best.items() if forecast_start is None or c < forecast_start}
    return historical, header_row, forecast_start


def _empty_fraction(ws, year_cols: dict[int, int], header_row: int) -> float:
    total = empty = 0
    for r in range(header_row + 1, ws.max_row + 1):
        label = ws.cell(r, 1).value
        if not isinstance(label, str) or not label.strip():
            continue
        for c in year_cols:
            total += 1
            if _is_empty(ws.cell(r, c)) and not _is_formula(ws.cell(r, c)):
                empty += 1
    return (empty / total) if total else 0.0


def _sheet_statement_type(ws, header_row: int, classifier: TableClassifier) -> StatementType | None:
    title = ws.cell(1, 1).value or ""
    labels = [
        str(ws.cell(r, 1).value)
        for r in range(header_row + 1, min(ws.max_row, header_row + 25) + 1)
        if isinstance(ws.cell(r, 1).value, str)
    ]
    signature = f"{title} {ws.title} " + " ".join(labels)
    result = classifier.classify(signature)
    return None if result.needs_review else result.statement_type


# --- candidates from extracted data ---

def _build_candidates(company: CompanyResult):
    """Return (by_type, pooled): lists of (label_norm, section_norm, line) per type."""
    by_type: dict[StatementType, list[tuple[str, str, object]]] = {}
    pooled: list[tuple[str, str, object]] = []
    for table in company.tables:
        bucket = by_type.setdefault(table.statement_type, [])
        for li in table.line_items:
            entry = (spaced(li.label), spaced(li.section or ""), li)
            bucket.append(entry)
            pooled.append(entry)
    return by_type, pooled


def _best_candidate(template_label: str, template_section: str, candidates, threshold: float):
    """Best extracted line for a template row, gated by section similarity.

    When both the template row and a candidate carry a section, the sections
    must be similar — this stops 'Export sales > Tractors' from matching the
    'Local sales > Tractors' line (and leaves export blank when the source has
    no export breakdown).
    """
    from rapidfuzz import fuzz

    q_label, q_section = spaced(template_label), spaced(template_section)
    best, best_score = None, 0.0
    for cand_label, cand_section, line in candidates:
        if not _section_compatible(q_section, cand_section):
            continue  # distinctive sections differ -> not a match
        score = fuzz.token_set_ratio(q_label, cand_label)
        if score > best_score:
            best, best_score = line, score
    return (best, best_score) if best_score >= threshold else (None, 0.0)


# --- main ---

def build_plan(company: CompanyResult, template_path: str | Path) -> MappingPlan:
    import openpyxl

    settings = get_settings()
    threshold = settings.template_match_threshold * 100
    classifier = TableClassifier()
    by_type, pooled = _build_candidates(company)

    wb = openpyxl.load_workbook(template_path, data_only=False)
    plan = MappingPlan()
    try:
        for ws in wb.worksheets:
            year_cols, header_row, _ = _detect_year_columns(ws)
            if len(year_cols) < 2:
                plan.sheets_skipped.append(ws.title)
                continue
            if _empty_fraction(ws, year_cols, header_row) < settings.template_min_empty_fraction:
                plan.sheets_skipped.append(ws.title)  # computed/output sheet
                continue

            plan.sheets_processed.append(ws.title)
            st = _sheet_statement_type(ws, header_row, classifier)
            candidates = by_type.get(st) or pooled

            template_section = ""  # carried forward from section-header rows
            for r in range(header_row + 1, ws.max_row + 1):
                label = ws.cell(r, 1).value
                if not isinstance(label, str) or not label.strip():
                    continue
                lbl = label.strip()
                if _is_section_header(lbl):
                    template_section = lbl  # e.g. LOCAL SALES / EXPORT SALES
                    continue
                writable = {c: y for c, y in year_cols.items()
                            if _is_empty(ws.cell(r, c)) and not _is_formula(ws.cell(r, c))}
                if not writable or not candidates:
                    continue

                line, score = _best_candidate(lbl, template_section, candidates, threshold)
                if line is None:
                    plan.unmatched_template_labels.append(lbl)
                    continue
                by_year = {v.year: v for v in line.values}
                for col, year in writable.items():
                    v = by_year.get(year)
                    if v is None or v.value is None:
                        continue
                    plan.writes.append(CellWrite(
                        sheet=ws.title,
                        coordinate=ws.cell(r, col).coordinate,
                        year=year,
                        value=v.value,
                        template_label=lbl,
                        matched_label=line.label,
                        confidence=score / 100.0,
                        source_report_year=v.source_report_year,
                    ))
    finally:
        wb.close()

    logger.info(
        "Template plan: %d writes across %d sheet(s); %d skipped, %d unmatched labels",
        len(plan.writes), len(plan.sheets_processed), len(plan.sheets_skipped),
        len(plan.unmatched_template_labels),
    )
    return plan


def apply_plan(plan: MappingPlan, template_path: str | Path, output_path: str | Path) -> Path:
    """Write the plan into a copy of the template (formulas/forecast untouched)."""
    import openpyxl

    wb = openpyxl.load_workbook(template_path, data_only=False)
    try:
        for w in plan.writes:
            wb[w.sheet][w.coordinate] = w.value
        wb.save(output_path)
    finally:
        wb.close()
    logger.info("Wrote %d values into %s", len(plan.writes), output_path)
    return Path(output_path)
