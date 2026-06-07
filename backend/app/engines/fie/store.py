"""FinancialFactStore — the L0 in-memory model.

Loads a delivered workbook into:
  - findata: pandas DataFrame (long) — statements + detail
  - source_ledger / validation_ledger: pandas DataFrame
  - insights: list[dict] (JSON)
  - manifest: dict

Provides lookup / detail / insights / cite primitives. No reasoning, no LLM.
See docs/fie_phase0_foundation.md §3, §5, §6.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

_log = logging.getLogger("app.engines.fie")

import openpyxl
import pandas as pd

from .ingest.classify import classify_sheet
from .ingest.formulas import WorkbookEvaluator
from .ingest.insights import parse_insights
from .ingest.ledgers import parse_source_ledger, parse_validation_ledger
from .ingest.statements import parse_grid_sheet
from .models import Citation, FactRef
from .ontology import STATEMENT_LINE_TO_DETAIL, MetricOntology, normalize_label

_UNIT = "Rupees in thousand"
_FINDATA_COLS = [
    "company", "statement", "level", "sheet", "cell", "label", "section",
    "metric", "note_ref", "year", "period_type", "value",
]


class FinancialFactStore:
    def __init__(
        self,
        *,
        company: str,
        findata: pd.DataFrame,
        source_ledger: pd.DataFrame,
        validation_ledger: pd.DataFrame,
        insights: list[dict],
        manifest: dict,
        ontology: MetricOntology,
    ) -> None:
        self.company = company
        self.unit = _UNIT
        self.findata = findata
        self.source_ledger = source_ledger
        self.validation_ledger = validation_ledger
        self._insights = insights
        self.manifest = manifest
        self.ontology = ontology
        self._cite_seq = 0
        self._cached_query_matcher: list | None = None

    # ------------------------------------------------------------------ load
    @classmethod
    def from_workbook(
        cls, path: str, *, manifest_path: str | None = None,
        ontology: MetricOntology | None = None,
    ) -> "FinancialFactStore":
        onto = ontology or MetricOntology()
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
        # second handle (formulas) to compute uncalculated statement cells
        wb_formulas = openpyxl.load_workbook(path, data_only=False, read_only=False)
        evaluator = WorkbookEvaluator(wb_formulas, wb)

        records: list[dict] = []
        source_ledger = pd.DataFrame()
        validation_ledger = pd.DataFrame()
        insights: list[dict] = []

        for ws in wb.worksheets:
            kind = classify_sheet(ws.title)
            if kind in ("statement", "detail"):
                level = "headline" if kind == "statement" else "detail"
                getter = (lambda coord, _t=ws.title: evaluator.value(_t, coord))
                records += parse_grid_sheet(ws, level=level, ontology=onto,
                                            value_getter=getter)
            elif kind == "source_ledger":
                source_ledger = parse_source_ledger(ws)
            elif kind == "validation_ledger":
                validation_ledger = parse_validation_ledger(ws)
            elif kind == "insights":
                insights += parse_insights(ws, id_prefix="INS")
            elif kind == "insights_review":
                insights += parse_insights(ws, id_prefix="INSR")

        manifest = cls._load_manifest(path, manifest_path)
        company = manifest.get("company") or os.path.basename(path)

        findata = pd.DataFrame(records, columns=_FINDATA_COLS)
        findata["company"] = company

        return cls(
            company=company, findata=findata, source_ledger=source_ledger,
            validation_ledger=validation_ledger, insights=insights,
            manifest=manifest, ontology=onto,
        )

    @staticmethod
    def _load_manifest(path: str, manifest_path: str | None) -> dict:
        mp = manifest_path
        if mp is None:
            base, _ = os.path.splitext(path)
            mp = base + ".manifest.json"
        if os.path.exists(mp):
            with open(mp, encoding="utf-8") as fh:
                return json.load(fh)
        return {}

    # ------------------------------------------------------------- introspect
    @property
    def years(self) -> list[int]:
        if self.findata.empty:
            return []
        return sorted(int(y) for y in self.findata["year"].dropna().unique())

    # --------------------------------------------------------------- lookups
    def lookup(
        self, metric: str, year: int, *,
        level: str = "headline", period_type: str = "historical",
    ) -> FactRef:
        df = self.findata
        sel = df[
            (df["metric"] == metric) & (df["year"] == year)
            & (df["level"] == level) & (df["period_type"] == period_type)
        ]
        if sel.empty:
            raise KeyError(f"no {level} fact for metric={metric!r} year={year} ({period_type})")
        # prefer a row with a value; otherwise the first match
        valued = sel[sel["value"].notna()]
        row = (valued.iloc[0] if not valued.empty else sel.iloc[0]).to_dict()
        return self._row_to_factref(row)

    def query_metric_matcher(self) -> list:
        """Compiled (pattern, canonical_id) list for query-time metric matching.

        Built once from the ontology aliases filtered to metrics actually present
        in this workbook.  Cached on the store instance — zero overhead after the
        first call.  Pass to understanding.understand() and metric_resolve.resolve()
        so query classification is driven by the uploaded Excel, not a hardcoded list.
        """
        if self._cached_query_matcher is None:
            self._cached_query_matcher = self.ontology.build_query_matcher(
                self.available_metrics()
            )
            _log.debug(
                "fie query_matcher cached for company=%r: %d patterns",
                self.company, len(self._cached_query_matcher),
                extra={"component": "Store"},
            )
        return self._cached_query_matcher

    def available_metrics(self, *, level: str = "headline",
                          period_type: str = "historical") -> set[str]:
        """Canonical metrics actually present (with a value) for this level/period —
        used to availability-gate query metric resolution."""
        df = self.findata
        sel = df[(df["level"] == level) & (df["period_type"] == period_type)
                 & df["value"].notna() & df["metric"].notna()]
        return set(sel["metric"].unique())

    def detail(
        self, *, sheet: str | None = None, metric: str | None = None,
        year: int | None = None,
    ) -> pd.DataFrame:
        df = self.findata[self.findata["level"] == "detail"]
        if sheet is not None:
            df = df[df["sheet"] == sheet]
        if metric is not None:
            df = df[df["metric"] == metric]
        if year is not None:
            df = df[df["year"] == year]
        return df.reset_index(drop=True)

    def insights(
        self, *, area: str | None = None, year: int | None = None,
        min_confidence: float = 0.0, include_review: bool = True,
    ) -> list[dict]:
        out = []
        for rec in self._insights:
            if not include_review and rec["insight_id"].startswith("INSR"):
                continue
            if area is not None and (rec.get("area") or "").lower() != area.lower():
                continue
            if year is not None and rec.get("year") != year:
                continue
            if (rec.get("confidence") or 0.0) < min_confidence:
                continue
            out.append(rec)
        return out

    def data_quality_flags(self, metric: str, years: list[int]) -> dict[int, str]:
        """Validation-ledger statuses for (metric, year) that are NOT clean.

        Dev-time signal surfaced only as an informational caveat (architecture §0.3);
        it never gates or overrides a runtime value.
        """
        vl = self.validation_ledger
        if vl is None or vl.empty or "Metric" not in vl.columns:
            return {}
        out: dict[int, str] = {}
        sub = vl[vl["Metric"] == metric]
        for _, r in sub.iterrows():
            try:
                y = int(r.get("Year"))
            except (TypeError, ValueError):
                continue
            status = str(r.get("Status") or "").strip()
            if y in years and status and status.lower() not in ("ok", "clean"):
                out[y] = status
        return out

    # ------------------------------------------------------------ provenance
    def cite(self, fact: FactRef) -> list[Citation]:
        """Resolve provenance into Citation objects (§6).

        Tiers: (1) direct Source Ledger hit, (2) headline via contributing detail
        sheet, (3) workbook-cell fallback when no Source Ledger is present.
        """
        sl = self.source_ledger
        if sl is not None and not sl.empty:
            # tier 1: direct (sheet, cell) hit  — true for detail rows
            direct = sl[(sl.get("Sheet") == fact.sheet) & (sl.get("Cell") == fact.cell)]
            if not direct.empty:
                return self._rows_to_citations(direct, basis="direct")

        # tier 2: headline -> via the contributing detail sheet
        if (sl is not None and not sl.empty
                and fact.level == "headline" and fact.metric in STATEMENT_LINE_TO_DETAIL):
            detail_sheet = STATEMENT_LINE_TO_DETAIL[fact.metric]
            label_norm = normalize_label(fact.label)
            cand = sl[(sl.get("Sheet") == detail_sheet) & (sl.get("Year") == fact.year)]
            # prefer an exact label match (a real total row); else the whole note
            if "matched_label_norm" in cand.columns:
                exact = cand[cand["matched_label_norm"] == label_norm]
                cand = exact if not exact.empty else cand
            # A1: the displayed headline value reflects the NEWEST report; cite only
            # that report's rows (older rows back the as-first-reported value), then
            # collapse to distinct source locations rather than one cite per line item.
            cand = self._newest_report_rows(cand)
            if not cand.empty:
                return self._rows_to_citations(cand, basis="via_detail", collapse=True)

        # tier 3: workbook-cell fallback (e.g. no-template OCR workbooks have no
        # Source Ledger) — the value's provenance is the workbook cell itself.
        if fact.sheet and fact.cell and fact.value is not None:
            self._cite_seq += 1
            return [Citation(
                ref_id=f"C{self._cite_seq}", kind="financial",
                display=f"{self.company} workbook: {fact.sheet}!{fact.cell}",
                locator={"sheet": fact.sheet, "cell": fact.cell, "year": fact.year,
                         "basis": "workbook"},
            )]
        return []

    @staticmethod
    def _newest_report_rows(rows: pd.DataFrame) -> pd.DataFrame:
        if rows.empty or "Report year" not in rows.columns:
            return rows
        ry = pd.to_numeric(rows["Report year"], errors="coerce")
        if ry.notna().any():
            return rows[ry == ry.max()]
        return rows

    def _rows_to_citations(self, rows: pd.DataFrame, *, basis: str,
                           collapse: bool = False) -> list[Citation]:
        """Build Citations from Source Ledger rows.

        When ``collapse`` is set, rows are grouped by distinct source location
        (report_file, page, table_id) so a derived total cites the source
        table/pages once each, not once per underlying line item (A1 decision).
        """
        cites: list[Citation] = []
        if rows.empty:
            return cites

        def _loc_key(r) -> tuple:
            return (r.get("Report file"), r.get("Page"), r.get("Table id"))

        if collapse:
            groups: dict[tuple, list] = {}
            order: list[tuple] = []
            for _, r in rows.iterrows():
                k = _loc_key(r)
                if k not in groups:
                    groups[k] = []
                    order.append(k)
                groups[k].append(r)
            buckets = [(k, groups[k]) for k in order]
        else:
            buckets = [(_loc_key(r), [r]) for _, r in rows.iterrows()]

        for _, group in buckets:
            r = group[0]
            self._cite_seq += 1
            rf, pg, ry = r.get("Report file"), r.get("Page"), r.get("Report year")
            display = str(rf) if rf is not None else "workbook"
            if pg is not None and pd.notna(pg):
                display = f"{display}, p{int(pg)}"
            # surface the SOURCE report year (vs the value year) so restatement
            # provenance is legible — "<file>, p12 (as reported FY2025)".
            if ry is not None and pd.notna(ry):
                display = f"{display} (as reported FY{int(ry)})"
            confs = [float(g["Confidence"]) for g in group
                     if "Confidence" in g and pd.notna(g.get("Confidence"))]
            cites.append(Citation(
                ref_id=f"C{self._cite_seq}",
                kind="financial",
                display=display,
                locator={
                    "report_file": rf, "page": pg,
                    "table_id": r.get("Table id"),
                    "sheet": r.get("Sheet"),
                    "cell": (r.get("Cell") if len(group) == 1 else None),
                    "year": r.get("Year"), "report_year": ry,
                    "basis": basis,
                    "derived_from_rows": len(group),
                },
                confidence=(min(confs) if confs else None),
            ))
        return cites

    # ------------------------------------------------------------- internals
    def _row_to_factref(self, row: dict) -> FactRef:
        fact = FactRef(
            company=self.company,
            metric=row.get("metric"),
            label=row.get("label") or "",
            year=int(row["year"]),
            period_type=row.get("period_type") or "historical",
            value=(None if pd.isna(row.get("value")) else row.get("value")),
            unit=self.unit,
            statement=row["statement"],
            level=row["level"],
            sheet=row["sheet"],
            cell=row["cell"],
        )
        # attach primary provenance (newest report_year) without resolving conflicts
        cites = self.cite(fact)
        if cites:
            primary = max(cites, key=lambda c: (c.locator.get("report_year") or 0))
            fact.source_ref = (
                primary.locator.get("table_id")
                or f"{primary.locator.get('report_file')}:p{primary.locator.get('page')}"
            )
            fact.report_year = primary.locator.get("report_year")
            fact.provenance_basis = primary.locator.get("basis", "none")
        return fact

    def coverage(self) -> dict:
        df = self.findata
        headline = df[df["level"] == "headline"]
        headline_metrics = headline[headline["metric"].notna()]
        # how many distinct headline (metric, year) resolve provenance
        citeable = 0
        checked = 0
        for (_, _), grp in headline_metrics.groupby(["metric", "year"]):
            checked += 1
            row = grp.iloc[0].to_dict()
            if self.cite(self._row_to_factref_noprov(row)):
                citeable += 1
        return {
            "cells_parsed": int(len(df)),
            "rows_with_value": int(df["value"].notna().sum()),
            "metrics_mapped": int(df["metric"].notna().sum()),
            "metrics_unmapped": int(df["metric"].isna().sum()),
            "headline_metric_years": checked,
            "headline_metric_years_citeable": citeable,
            "source_ledger_rows": int(len(self.source_ledger)),
            "insights": len(self._insights),
            "years": self.years,
        }

    def _row_to_factref_noprov(self, row: dict) -> FactRef:
        return FactRef(
            company=self.company, metric=row.get("metric"), label=row.get("label") or "",
            year=int(row["year"]), period_type=row.get("period_type") or "historical",
            value=(None if pd.isna(row.get("value")) else row.get("value")),
            unit=self.unit, statement=row["statement"], level=row["level"],
            sheet=row["sheet"], cell=row["cell"],
        )
