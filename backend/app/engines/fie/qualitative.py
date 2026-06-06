"""Qualitative taxonomy → themes → coverage gate (L5/L6).

Ported from the legacy QAE: a deterministic, LLM-free pipeline that turns our flat
"insights" into a structured, coverage-audited view over a frozen 6-category /
27-theme PSX taxonomy (``qualitative_taxonomy.json``).

  1. CANONICALIZE  free-text insight `area` (+ takeaway/section) → a taxonomy theme
                   via a 4-tier ladder: exact (1.0) → alias (0.9) → keyword (0.65)
                   → unmapped (0.0), with a min-floor mapping confidence and a
                   section-prior consistency check.
  2. ASSEMBLE      group mapped insights by theme; score three orthogonal axes —
                   theme_confidence, evidence_weight, materiality — and surface
                   intra-theme directional DIVERGENCE (never auto-resolved).
  3. COVER         per-category coverage gate that distinguishes "no risk found"
                   from "risk section never read" (expected-section presence).

Operates on our insight dicts ({insight_id, area, takeaway, source_section, page,
year, confidence}); no engine models are imported, so it is self-contained.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_TAXONOMY_PATH = Path(__file__).parent / "qualitative_taxonomy.json"


# --- text normalization (legacy parity) ------------------------------------
def normalize_text(value: str) -> str:
    s = (value or "").lower().replace("&", " and ").replace("_", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --- section → category priors --------------------------------------------
_SECTION_PRIORS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chairman review": ("outlook", ("strategy", "governance")),
    "ceo review": ("outlook", ("strategy",)),
    "directors report": ("governance", ("outlook", "strategy")),
    "management discussion and analysis": ("strategy", ("outlook", "business_risk")),
    "business review": ("strategy", ("operational_risk", "outlook")),
    "risks": ("business_risk", ("operational_risk",)),
    "opportunities": ("strategy", ("outlook",)),
    "outlook": ("outlook", ()),
    "strategy": ("strategy", ("outlook",)),
    "financial review": ("business_risk", ("outlook",)),
    "sustainability": ("esg", ()),
    "esg": ("esg", ()),
}
_ROUTE_CONFIDENCE = 0.9

# expected source sections per category — absence of these flags weak coverage
_EXPECTED_SECTIONS: dict[str, tuple[str, ...]] = {
    "outlook": ("chairman review", "ceo review", "directors report",
                "management discussion and analysis", "business review",
                "opportunities", "outlook", "strategy", "financial review"),
    "strategy": ("chairman review", "ceo review", "directors report",
                 "management discussion and analysis", "business review",
                 "opportunities", "strategy"),
    "business_risk": ("management discussion and analysis", "risks", "financial review"),
    "operational_risk": ("business review", "risks"),
    "governance": ("chairman review", "directors report"),
    "esg": ("sustainability", "esg"),
}

# --- mapping-confidence composition ---------------------------------------
_METHOD_CONF = {"exact": 1.0, "alias": 0.9, "keyword": 0.65, "section_only": 0.35, "unmapped": 0.0}
_SECTION_CONFLICT_PENALTY = 0.15

# --- theme scoring constants (legacy parity) ------------------------------
_CATEGORY_MATERIALITY_PRIOR = {
    "business_risk": 0.65, "governance": 0.65, "operational_risk": 0.58,
    "esg": 0.55, "strategy": 0.52, "outlook": 0.48,
}
_AUTHORITY_WEIGHT = 0.9            # insights = audited issuer narrative
_DIVERGENCE_CONF_IMPACT = 0.15
_DIVERGENCE_MAT_IMPACT = 0.15

_POSITIVE_TERMS = ("increase", "increased", "growth", "grew", "higher", "improved",
                   "expanded", "surged", "recovery", "strong")
_NEGATIVE_TERMS = ("decline", "declined", "decrease", "decreased", "lower", "fell",
                   "reduced", "weak", "slowdown", "pressure", "risk")


@lru_cache(maxsize=1)
def _taxonomy() -> dict:
    """Load + index the taxonomy once. Indexes mirror the legacy loader:
    exact (normalized theme_ref), alias (aliases + example labels), keyword
    (same keys, phrase-containment, longest-wins)."""
    payload = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    categories = {c["category_ref"]: c for c in payload["categories"]}
    themes = {t["theme_ref"]: t for t in payload["themes"]}
    exact_index = {normalize_text(ref): ref for ref in themes}
    alias_index: dict[str, str] = {}
    keyword_index: dict[str, list[str]] = {}
    for t in themes.values():
        for phrase in (*t.get("aliases", []), *t.get("example_area_labels", [])):
            n = normalize_text(phrase)
            if not n:
                continue
            alias_index.setdefault(n, t["theme_ref"])
            keyword_index.setdefault(n, []).append(t["theme_ref"])
    return {"version": payload["taxonomy_version"], "categories": categories,
            "themes": themes, "exact": exact_index, "alias": alias_index,
            "keyword": keyword_index}


def _route_section(source_section: str | None) -> dict:
    n = normalize_text(source_section or "")
    pri = _SECTION_PRIORS.get(n)
    if pri is None:
        return {"recognized": False, "primary": None, "secondary": (), "norm": n}
    return {"recognized": True, "primary": pri[0], "secondary": pri[1], "norm": n}


def _match_keyword(haystack: str, keyword_index: dict) -> tuple[str | None, str | None]:
    """Longest-phrase-wins containment match over the keyword index."""
    padded = f" {haystack} "
    best: tuple[int, str, str] | None = None
    for kw, refs in keyword_index.items():
        if kw and f" {kw} " in padded:
            cand = (len(kw), kw, refs[0])
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] < best[1]):
                best = cand
    return (best[2], best[1]) if best else (None, None)


def canonicalize(area: str | None, *, takeaway: str | None = None,
                 source_section: str | None = None,
                 extraction_confidence: float = 1.0) -> dict:
    """Map a free-text area to a taxonomy theme via the 4-tier ladder. Returns
    {theme_ref, category_ref, secondary, mapping_method, mapping_confidence,
    confidence, section_conflict, unmapped, matched}."""
    tax = _taxonomy()
    norm = normalize_text(area or "")
    route = _route_section(source_section)

    method, theme_ref, matched = "unmapped", None, None
    if norm and norm in tax["exact"]:
        method, theme_ref, matched = "exact", tax["exact"][norm], norm
    elif norm and norm in tax["alias"]:
        method, theme_ref, matched = "alias", tax["alias"][norm], norm
    else:
        hay = f"{norm} {normalize_text(takeaway or '')}".strip()
        kref, kw = _match_keyword(hay, tax["keyword"])
        if kref:
            method, theme_ref, matched = "keyword", kref, kw

    if theme_ref is None:                                   # unmapped — category from section prior
        return {"theme_ref": None, "category_ref": route["primary"],
                "secondary": route["secondary"], "mapping_method": "unmapped",
                "mapping_confidence": 0.0, "confidence": 0.0,
                "section_conflict": False, "unmapped": True, "matched": None}

    theme = tax["themes"][theme_ref]
    sec_cats = set((route["primary"], *route["secondary"])) if route["recognized"] else set()
    conflict = bool(route["recognized"] and theme["category_ref"] not in sec_cats
                    and not (set(theme.get("secondary_categories", [])) & sec_cats))
    mapping_conf = _METHOD_CONF[method]
    inputs = [mapping_conf, extraction_confidence]
    if route["recognized"]:
        inputs.append(_ROUTE_CONFIDENCE)
    conf = min(inputs)
    if conflict:
        conf = max(0.0, conf - _SECTION_CONFLICT_PENALTY)
    return {"theme_ref": theme_ref, "category_ref": theme["category_ref"],
            "secondary": tuple(theme.get("secondary_categories", [])),
            "mapping_method": method, "mapping_confidence": mapping_conf,
            "confidence": round(conf, 6), "section_conflict": conflict,
            "unmapped": False, "matched": matched}


def enrich(insight: dict) -> dict:
    """Return a copy of an insight dict with canonical taxonomy fields added."""
    c = canonicalize(insight.get("area"), takeaway=insight.get("takeaway"),
                     source_section=insight.get("source_section"),
                     extraction_confidence=float(insight.get("confidence") or 1.0))
    return {**insight, **{f"_{k}": v for k, v in c.items()}}


def _direction(text: str) -> str | None:
    t = (text or "").lower()
    pos = any(w in t for w in _POSITIVE_TERMS)
    neg = any(w in t for w in _NEGATIVE_TERMS)
    return "positive" if pos and not neg else "negative" if neg and not pos else None


def _is_quantified(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


def assemble_themes(insights: list[dict]) -> list[dict]:
    """Group mapped insights by theme and score them. Sorted by materiality desc
    (non-dilutive). Unmapped insights are excluded (see ``coverage`` for those)."""
    tax = _taxonomy()
    enriched = [enrich(i) for i in insights]
    groups: dict[str, list[dict]] = {}
    for e in enriched:
        if e.get("_theme_ref"):
            groups.setdefault(e["_theme_ref"], []).append(e)

    out: list[dict] = []
    for theme_ref, sigs in groups.items():
        theme = tax["themes"][theme_ref]
        cat = theme["category_ref"]
        confs = [float(s.get("confidence") or 0.0) for s in sigs]
        takeaways = [s.get("takeaway") or "" for s in sigs]
        sections = {s.get("source_section") for s in sigs if s.get("source_section")}
        quantified = any(_is_quantified(t) for t in takeaways)

        # divergence: opposing directional language within the theme (surfaced, not resolved)
        dirs = {d for d in (_direction(t) for t in takeaways) if d}
        divergent = {"positive", "negative"} <= dirs

        # theme_confidence = max signal confidence − divergence penalty, with ceilings
        base = max(confs) if confs else 0.0
        penalty = _DIVERGENCE_CONF_IMPACT if divergent else 0.0
        ceiling = 0.7 if all(s.get("_mapping_method") == "keyword" for s in sigs) else 1.0
        theme_conf = round(max(0.0, min(ceiling, base - penalty)), 6)

        # evidence_weight = authority·0.65 + support + section-spread + specificity
        support_lift = min(0.15, (len(sigs) - 1) * 0.03)
        section_lift = min(0.15, max(0, len(sections) - 1) * 0.05)
        spec_lift = 0.05 if quantified else 0.0
        evidence_weight = round(min(1.0, _AUTHORITY_WEIGHT * 0.65
                                    + support_lift + section_lift + spec_lift), 6)

        # materiality = category prior + corroboration + quantified + divergence
        mat = _CATEGORY_MATERIALITY_PRIOR.get(cat, 0.5)
        mat += min(0.18, (len(sigs) - 1) * 0.03)
        if quantified:
            mat += 0.05
        if divergent:
            mat += _DIVERGENCE_MAT_IMPACT
        materiality = round(min(1.0, mat), 6)

        out.append({
            "theme_ref": theme_ref, "theme_name": theme.get("description", theme_ref),
            "category_ref": cat, "category_name": tax["categories"][cat]["name"],
            "insight_ids": [s.get("insight_id") for s in sigs],
            "signal_count": len(sigs),
            "theme_confidence": theme_conf, "evidence_weight": evidence_weight,
            "materiality": materiality, "divergent": divergent,
            "sections": sorted(sections), "takeaways": takeaways,
        })
    out.sort(key=lambda t: (t["materiality"], t["evidence_weight"]), reverse=True)
    return out


# --- coverage gate ---------------------------------------------------------
_CONF_FLOOR = 0.50
_UNMAPPED_WARN = 0.25
_MAPPED_COVERAGE_FLOOR = 50.0
_MIN_ELIGIBLE = 2


def coverage(insights: list[dict]) -> dict:
    """Per-category coverage gate + overall run status. Distinguishes 'no signal'
    from 'expected section absent' so the answer can say e.g. 'governance coverage:
    insufficient — Directors Report not read'."""
    tax = _taxonomy()
    enriched = [enrich(i) for i in insights]
    by_cat: dict[str, list[dict]] = {}
    for e in enriched:
        cat = e.get("_category_ref")
        if cat:
            by_cat.setdefault(cat, []).append(e)

    cats_out: dict[str, dict] = {}
    for cat in tax["categories"]:
        sigs = by_cat.get(cat, [])
        raw = len(sigs)
        mapped = [s for s in sigs if s.get("_theme_ref")]
        eligible = [s for s in mapped if float(s.get("confidence") or 0.0) >= _CONF_FLOOR]
        unmapped_rate = (raw - len(mapped)) / raw if raw else 0.0
        mapped_cov = (len(mapped) / raw * 100.0) if raw else 0.0
        present_sections = {normalize_text(s.get("source_section") or "") for s in sigs}
        expected = _EXPECTED_SECTIONS.get(cat, ())
        expected_present = [s for s in expected if s in present_sections]

        warnings: list[str] = []
        if not eligible:
            status = "SKIPPED_NO_ELIGIBLE_SIGNALS"
        elif mapped_cov < _MAPPED_COVERAGE_FLOOR:
            status = "SKIPPED_INSUFFICIENT_COVERAGE"
        else:
            if unmapped_rate > _UNMAPPED_WARN:
                warnings.append("high_unmapped_rate")
            if len(eligible) < _MIN_ELIGIBLE:
                warnings.append("few_eligible_signals")
            if raw > 0 and not expected_present:
                warnings.append("expected_section_absent")
            status = "ADMITTED_WITH_WARNING" if warnings else "ADMITTED"

        cats_out[cat] = {
            "status": status, "raw_signals": raw, "mapped": len(mapped),
            "eligible": len(eligible), "unmapped_rate": round(unmapped_rate, 4),
            "mapped_coverage_pct": round(mapped_cov, 2),
            "expected_sections_present": expected_present,
            "expected_section_absent": bool(raw and not expected_present),
            "warnings": warnings,
        }

    admitted = [c for c, v in cats_out.items()
                if v["status"] in ("ADMITTED", "ADMITTED_WITH_WARNING")]
    if not admitted:
        run_status = "INSUFFICIENT_COVERAGE"
    elif len(admitted) < len(cats_out) or any(
            v["status"] == "ADMITTED_WITH_WARNING" for v in cats_out.values()):
        run_status = "PARTIAL"
    else:
        run_status = "ANALYZED"

    unmapped = [e for e in enriched if not e.get("_theme_ref")]
    return {"run_status": run_status, "categories": cats_out,
            "admitted_categories": admitted,
            "unmapped_count": len(unmapped),
            "taxonomy_version": tax["version"]}
