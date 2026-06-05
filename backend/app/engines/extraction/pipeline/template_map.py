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
    "note", "notes",
}


# --- statement-type containment (lever 1) ---
# A breakdown sheet's data may be extracted under its parent FACE statement (or a
# sibling breakdown), e.g. "Cash in hand" landing in the whole `balance_sheet`
# table instead of the `current_assets` breakdown. So when a sheet's own type
# yields no match for a row, widen the candidate pool to its statement family.
_FACE_GROUPS: dict[StatementType, set[StatementType]] = {
    StatementType.balance_sheet: {
        StatementType.non_current_assets, StatementType.current_assets,
        StatementType.share_capital_reserves, StatementType.non_current_liabilities,
        StatementType.current_liabilities, StatementType.equity_changes,
    },
    StatementType.income_statement: {
        StatementType.revenue, StatementType.cost_of_sales, StatementType.operating_expenses,
        StatementType.other_income, StatementType.finance_cost, StatementType.taxation,
        StatementType.oci,
    },
}


def _related_types(st: StatementType | None) -> set[StatementType]:
    """The statement family a sheet's type belongs to: itself + its face + the
    face's other parts. Used as a widened (fallback) candidate pool."""
    if st is None:
        return set()
    related: set[StatementType] = {st}
    for face, parts in _FACE_GROUPS.items():
        if st == face or st in parts:
            related.add(face)
            related |= parts
    return related


# --- label normalization for matching (lever 2) ---
# Applied on top of `spaced()` so corporate/financial abbreviations don't sink a
# fuzzy match below threshold (e.g. "(Pvt) Ltd" vs "(Private) Limited").
_ABBREV = {
    "pvt": "private", "ltd": "limited", "co": "company", "corp": "corporation",
    "incl": "including", "amt": "amount", "recd": "received", "dep": "depreciation",
    "accum": "accumulated", "invt": "investment", "mfg": "manufacturing",
    "wppf": "workers profit participation fund", "wwf": "workers welfare fund",
}


def _match_norm(label: str) -> str:
    """Normalize a label for fuzzy matching: `spaced()` + abbreviation expansion."""
    return " ".join(_ABBREV.get(tok, tok) for tok in spaced(label).split())


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
    """Return (by_type, pooled): lists of (label_norm, section_norm, line) per type.

    Templates target the UNCONSOLIDATED set, so when a statement type has both
    sets we use the unconsolidated (or unknown) tables and skip the consolidated
    ones as mapping candidates.
    """
    from collections import defaultdict

    tables_by_type: dict[StatementType, list] = defaultdict(list)
    for table in company.tables:
        tables_by_type[table.statement_type].append(table)

    by_type: dict[StatementType, list[tuple[str, str, object]]] = {}
    pooled: list[tuple[str, str, object]] = []
    for st, tables in tables_by_type.items():
        preferred = [t for t in tables if t.consolidated is not True]  # False or None
        chosen = preferred or tables  # fall back to consolidated if that's all we have
        bucket: list[tuple[str, str, object]] = []
        for table in chosen:
            for li in table.line_items:
                entry = (_match_norm(li.label), spaced(li.section or ""), li)
                bucket.append(entry)
                pooled.append(entry)
        by_type[st] = bucket
    return by_type, pooled


def _section_overlap(template_section_norm: str, cand_section_norm: str) -> int:
    """Count of shared distinctive section tokens (0 = unrelated/uninformative)."""
    return len(_significant_tokens(template_section_norm) & _significant_tokens(cand_section_norm))


def _best_candidate(template_label: str, template_section: str, candidates, threshold: float):
    """Best extracted line for a template row by LABEL (recall-first), with the
    section used only as a tie-breaker. Conflicts where one extracted line is
    claimed by two template rows are resolved later in `build_plan`.
    """
    from rapidfuzz import fuzz

    q_label, q_section = _match_norm(template_label), spaced(template_section)
    best, best_label_score, best_combined = None, 0.0, -1.0
    for cand_label, cand_section, line in candidates:
        label_score = fuzz.token_set_ratio(q_label, cand_label)
        if label_score < threshold:
            continue
        # Prefer a section-matching candidate when labels tie (e.g. Local vs Export).
        combined = label_score + (5 if _section_overlap(q_section, cand_section) else 0)
        if combined > best_combined:
            best, best_label_score, best_combined = line, label_score, combined
    return (best, best_label_score) if best is not None else (None, 0.0)


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
            # Tier 1: the sheet's own statement type (precision-first).
            own = by_type.get(st) or []
            # Tier 2: widen to the statement family (face + sibling breakdowns) so a
            # line extracted into a parent/sibling table is still reachable; falls
            # back to the full pool only when the family yields nothing.
            widened: list = []
            for rt in _related_types(st):
                widened.extend(by_type.get(rt, []))
            widened = widened or pooled

            # Pass 1: best label match per writable leaf row (recall-first).
            matches = []  # dicts: ws, writable, lbl, template_section, line, score
            template_section = ""
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
                if not writable or not (own or widened):
                    continue
                # Own type first; only widen to the family when the sheet's own type
                # has no candidate for this row (keeps cross-statement bleed out).
                line, score = _best_candidate(lbl, template_section, own, threshold)
                if line is None:
                    line, score = _best_candidate(lbl, template_section, widened, threshold)
                if line is None:
                    plan.unmatched_template_labels.append(lbl)
                    continue
                matches.append({
                    "ws": ws, "row": r, "writable": writable, "lbl": lbl,
                    "template_section": template_section, "line": line, "score": score,
                })

            # Pass 2: resolve conflicts — if one extracted line is claimed by
            # several template rows (e.g. Local 'Tractors' and Export 'Tractors'),
            # keep the row whose section best matches; blank the others.
            from collections import defaultdict
            by_line: dict[int, list] = defaultdict(list)
            for m in matches:
                by_line[id(m["line"])].append(m)
            for group in by_line.values():
                if len(group) > 1:
                    winner = max(group, key=lambda m: (
                        _section_overlap(spaced(m["template_section"]), spaced(m["line"].section or "")),
                        m["score"],
                    ))
                    for m in group:
                        if m is not winner:
                            plan.unmatched_template_labels.append(m["lbl"])
                    group[:] = [winner]

            # Write the surviving matches.
            for m in (m for grp in by_line.values() for m in grp):
                by_year = {v.year: v for v in m["line"].values}
                for col, year in m["writable"].items():
                    v = by_year.get(year)
                    if v is None or v.value is None:
                        continue
                    plan.writes.append(CellWrite(
                        sheet=m["ws"].title,
                        coordinate=m["ws"].cell(m["row"], col).coordinate,
                        year=year, value=v.value, template_label=m["lbl"],
                        matched_label=m["line"].label, confidence=m["score"] / 100.0,
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
