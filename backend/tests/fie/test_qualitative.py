"""Qualitative taxonomy pipeline: 4-tier canonicalization, theme assembly
(confidence/evidence_weight/materiality + divergence), and the coverage gate."""

from app.engines.fie import qualitative as Q


def _ins(insight_id, area, takeaway, section, conf=0.8, year=2025, page=10):
    return {"insight_id": insight_id, "area": area, "takeaway": takeaway,
            "source_section": section, "confidence": conf, "year": year, "page": page}


# --- canonicalization tiers ------------------------------------------------
def test_canonicalize_exact_and_alias():
    # exact: normalized theme_ref
    ex = Q.canonicalize("capacity expansion", source_section="Strategy")
    assert ex["theme_ref"] == "capacity_expansion" and ex["mapping_method"] == "exact"
    assert ex["mapping_confidence"] == 1.0
    # alias: an example label / alias phrase
    al = Q.canonicalize("Cement Demand", source_section="Outlook")
    assert al["theme_ref"] == "demand_outlook" and al["mapping_method"] == "alias"
    assert al["mapping_confidence"] == 0.9


def test_canonicalize_keyword_and_unmapped():
    kw = Q.canonicalize("Some note", takeaway="rising coal price and energy cost pressure",
                        source_section="Risks")
    assert kw["mapping_method"] == "keyword" and kw["category_ref"] == "business_risk"
    um = Q.canonicalize("Totally unrelated label", takeaway="nothing matches here",
                        source_section="Mystery")
    assert um["unmapped"] is True and um["theme_ref"] is None and um["confidence"] == 0.0


def test_section_conflict_penalty():
    # a governance theme tagged under an Outlook section -> conflict penalty applied
    r = Q.canonicalize("board oversight", source_section="Outlook")
    assert r["theme_ref"] == "board_oversight" and r["section_conflict"] is True
    # exact(1.0) capped by section(0.9) then −0.15 penalty = 0.75
    assert r["confidence"] == 0.75


def test_min_floor_uses_extraction_confidence():
    r = Q.canonicalize("capacity expansion", source_section="Strategy", extraction_confidence=0.4)
    assert r["confidence"] == 0.4          # min(exact 1.0, section 0.9, extraction 0.4)


# --- theme assembly --------------------------------------------------------
def test_assemble_themes_scores_and_orders():
    ins = [
        _ins("i1", "Input Cost", "rising coal price pressure on margins", "Risks", conf=0.9),
        _ins("i2", "Energy Cost", "higher energy cost and fuel pressure", "Risks", conf=0.8),
        _ins("i3", "Demand Outlook", "tractor demand growth expected", "Outlook", conf=0.7),
    ]
    themes = Q.assemble_themes(ins)
    refs = {t["theme_ref"] for t in themes}
    assert "input_cost_energy" in refs and "demand_outlook" in refs
    ic = next(t for t in themes if t["theme_ref"] == "input_cost_energy")
    assert ic["signal_count"] == 2 and ic["category_ref"] == "business_risk"
    assert 0 < ic["materiality"] <= 1 and 0 < ic["theme_confidence"] <= 1
    # business_risk prior (0.65) outranks outlook (0.48) -> sorted first
    assert themes[0]["category_ref"] == "business_risk"


def test_divergence_surfaced_not_resolved():
    ins = [
        _ins("i1", "Demand Outlook", "demand growth and higher volumes expected", "Outlook"),
        _ins("i2", "Demand Outlook", "demand decline; weaker sales and lower volumes", "Outlook"),
    ]
    themes = Q.assemble_themes(ins)
    t = next(t for t in themes if t["theme_ref"] == "demand_outlook")
    assert t["divergent"] is True                      # opposing direction -> flagged
    assert t["signal_count"] == 2


# --- coverage gate ---------------------------------------------------------
def test_coverage_admits_well_covered_category():
    ins = [
        _ins("i1", "Input Cost", "coal price pressure", "Risks", conf=0.9),
        _ins("i2", "Regulatory", "new tax and regulatory duty risk", "Risks", conf=0.8),
    ]
    cov = Q.coverage(ins)
    assert cov["categories"]["business_risk"]["status"] in ("ADMITTED", "ADMITTED_WITH_WARNING")
    assert cov["run_status"] in ("ANALYZED", "PARTIAL")


def test_coverage_flags_low_confidence_as_no_eligible():
    ins = [_ins("i1", "Input Cost", "coal price", "Risks", conf=0.2)]   # below 0.50 floor
    cov = Q.coverage(ins)
    assert cov["categories"]["business_risk"]["status"] == "SKIPPED_NO_ELIGIBLE_SIGNALS"
    # governance never had any signal -> also skipped (distinct from 'no risk')
    assert cov["categories"]["governance"]["status"] == "SKIPPED_NO_ELIGIBLE_SIGNALS"


def test_coverage_expected_section_absent_warning():
    # a governance theme found OUTSIDE its expected sections (no Directors Report read)
    ins = [
        _ins("i1", "Board Oversight", "board independence improved", "Risks", conf=0.9),
        _ins("i2", "Internal Controls", "controls strengthened", "Risks", conf=0.8),
    ]
    cov = Q.coverage(ins)
    gov = cov["categories"]["governance"]
    assert gov["expected_section_absent"] is True
