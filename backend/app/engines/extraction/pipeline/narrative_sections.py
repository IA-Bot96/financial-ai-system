"""Identify business-narrative sections (CEO/Chairman/Directors/MD&A/Outlook…)
and exclude boilerplate, so insight extraction only sees high-signal prose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.logging import get_logger
from app.engines.extraction.models.document import IngestedDoc
from app.engines.extraction.services.narrative_keywords import (
    NARRATIVE_SECTIONS,
    TERMINAL_KEYWORDS,
)

logger = get_logger(__name__)

_HEADING_SCAN_LINES = 6
_MAX_HEADING_LEN = 70
_MIN_PROSE_CHARS = 120     # letters required for a page to count as narrative
_MAX_DIGIT_RATIO = 0.35    # above this the page is table-like -> skip
_NONALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase and collapse non-alphanumerics to spaces (mojibake/punct safe)."""
    return _NONALNUM.sub(" ", text.lower()).strip()


@dataclass(frozen=True)
class NarrativePage:
    page_number: int
    section: str
    text: str


def _first_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()][:_HEADING_SCAN_LINES]


def _match_alias(text: str) -> str | None:
    """Match a normalized heading candidate (first line, or first two joined)."""
    lines = _first_lines(text)
    candidates = []
    if lines:
        candidates.append(_normalize(lines[0]))
        if len(lines) > 1:
            candidates.append(_normalize(lines[0] + " " + lines[1]))
    for cand in candidates:
        if not cand or len(cand) > _MAX_HEADING_LEN:
            continue
        for section, aliases in NARRATIVE_SECTIONS.items():
            if any(cand.startswith(a) for a in aliases):
                return section
    return None


def _is_heading_line(line: str) -> bool:
    """A short, mostly-uppercase line marks a (possibly new) section boundary."""
    s = line.strip()
    if not s or len(s) > _MAX_HEADING_LEN:
        return False
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 3:
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.7


def _is_terminal(low_text: str) -> bool:
    return any(k in low_text for k in TERMINAL_KEYWORDS)


def _is_prose(text: str) -> bool:
    letters = sum(c.isalpha() for c in text)
    if letters < _MIN_PROSE_CHARS:
        return False
    digits = sum(c.isdigit() for c in text)
    return (digits / max(1, letters + digits)) <= _MAX_DIGIT_RATIO


def identify_narrative_sections(doc: IngestedDoc) -> list[NarrativePage]:
    """Assign narrative pages to a section. A section starts at a matching
    heading and continues across following prose pages until the next heading
    line (matched or not) or a boilerplate/terminal page ends it — so genuine
    multi-page sections are kept without sweeping in unrelated content."""
    out: list[NarrativePage] = []
    current: str | None = None

    for page in doc.pages:
        low = page.text.lower()
        matched = _match_alias(page.text)
        first_lines = _first_lines(page.text)
        starts_with_heading = bool(first_lines) and _is_heading_line(first_lines[0])

        if matched:
            current = matched
        elif _is_terminal(low):
            current = None
            continue
        elif starts_with_heading:
            # A new, non-narrative section heading -> stop the previous section.
            current = None
            continue

        if current and _is_prose(page.text):
            out.append(NarrativePage(page.page, current, page.text))

    logger.info("Identified %d narrative page(s) in %s", len(out), doc.file_name)
    return out
