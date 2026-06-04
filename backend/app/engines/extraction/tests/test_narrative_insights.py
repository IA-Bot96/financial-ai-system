"""Tests for narrative section identification + chunking (insight front-half)."""
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.pipeline import insight_chunks as ic
from app.engines.extraction.pipeline.narrative_sections import (
    _match_alias,
    identify_narrative_sections,
)

_PROSE = (
    "Export volumes rose sharply supported by strong global demand while the "
    "company expanded capacity and managed working capital, debt and margins "
    "prudently across all of its key export markets during the year under review."
)


def test_match_alias_handles_mojibake_and_message():
    assert _match_alias("CEO’S MESSAGE\nDear shareholders") == "CEO Review"
    assert _match_alias("CEO�S MESSAGE") == "CEO Review"   # mojibake apostrophe
    assert _match_alias("Chairman's Review") == "Chairman Review"
    assert _match_alias("Business Review") == "Business Review"
    assert _match_alias("ROAD TO SUCCESS") is None


def _page(n, text):
    return PageText(page=n, text=text, kind=PageKind.native, char_count=len(text))


def test_continuation_stops_at_new_heading_and_terminal():
    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [
        _page(1, f"CEO'S MESSAGE\n{_PROSE}"),          # CEO Review
        _page(2, _PROSE),                               # continuation -> CEO Review
        _page(3, f"ROAD TO SUCCESS\n1993 incorporated"),  # new heading -> stop
        _page(4, f"Notes to the financial statements\n{_PROSE}"),  # terminal
    ]
    doc.page_count = 4
    pages = identify_narrative_sections(doc)
    assert [(p.page_number, p.section) for p in pages] == [(1, "CEO Review"), (2, "CEO Review")]


def test_table_heavy_page_is_not_prose():
    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [
        _page(1, "CEO'S MESSAGE\n" + " ".join(["1,234"] * 60)),  # numbers, not prose
    ]
    doc.page_count = 1
    assert identify_narrative_sections(doc) == []


def test_rank_chunks_prefers_high_weight_section():
    chunks = [
        ic.NarrativeChunk(5, "Sustainability", "energy and esg matters"),
        ic.NarrativeChunk(2, "Management Discussion & Analysis", "revenue margin debt exports"),
    ]
    ranked = ic.rank_chunks(chunks)
    assert ranked[0].source_section == "Management Discussion & Analysis"


def test_build_chunks_overlaps_long_text():
    long_text = "para one. " + "x" * 3000
    pages = [type("P", (), {"page_number": 1, "section": "CEO Review", "text": long_text})()]
    chunks = ic.build_chunks(pages, max_chars=2800, overlap=250)
    assert len(chunks) >= 2
