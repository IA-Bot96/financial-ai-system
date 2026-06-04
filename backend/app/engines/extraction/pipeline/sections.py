"""Section detection (rule-based) — maps statement headings to page ranges."""
from __future__ import annotations

import re

from app.core.logging import get_logger
from app.engines.extraction.models.common import StatementType
from app.engines.extraction.models.document import IngestedDoc
from app.engines.extraction.models.table import Section
from app.engines.extraction.services.keywords import SECTION_HEADINGS

logger = get_logger(__name__)

# Headings usually appear near the top of a page and on a short line.
_MAX_HEADING_LINE_LEN = 80
_HEADING_SCAN_LINES = 12
# Strip leading numbering / bullets so "1. Balance Sheet" still anchors.
_LEADING_MARKERS = re.compile(r"^[\s\d.)\-•:]+")
# Qualifiers that may precede a statement heading; map to consolidated flag.
_QUALIFIERS: list[tuple[str, bool | None]] = [
    ("unconsolidated", False),
    ("consolidated", True),
    ("separate", False),
    ("condensed interim", None),
]


def _match_heading(line: str) -> tuple[StatementType, str, bool | None] | None:
    low = line.strip().lower()
    if not low or len(low) > _MAX_HEADING_LINE_LEN:
        return None
    # Require the heading phrase at the START of the line (after any numbering),
    # so body text like "more balance sheet rows" is not treated as a heading.
    anchored = _LEADING_MARKERS.sub("", low)

    # Strip an optional leading qualifier (Unconsolidated/Consolidated/...).
    consolidated: bool | None = None
    for q, flag in _QUALIFIERS:
        if anchored.startswith(q + " "):
            consolidated = flag
            anchored = anchored[len(q) + 1:]
            break

    for st, headings in SECTION_HEADINGS.items():
        for h in headings:
            if anchored.startswith(h):
                return st, line.strip(), consolidated
    return None


def detect_sections(doc: IngestedDoc) -> list[Section]:
    """Find statement headings and assign each a [start_page, end_page] range.

    A section runs from its heading page up to the page before the next heading.
    """
    hits: list[tuple[int, StatementType, str, bool | None]] = []
    for page in doc.pages:
        lines = page.text.splitlines()[:_HEADING_SCAN_LINES]
        for line in lines:
            match = _match_heading(line)
            if match:
                hits.append((page.page, match[0], match[1], match[2]))
                break  # one heading per page is enough to anchor a section

    if not hits:
        return []

    sections: list[Section] = []
    last_page = doc.page_count or (doc.pages[-1].page if doc.pages else 1)
    for i, (page, st, title, consolidated) in enumerate(hits):
        end = (hits[i + 1][0] - 1) if i + 1 < len(hits) else last_page
        sections.append(
            Section(
                statement_type=st, title=title, consolidated=consolidated,
                start_page=page, end_page=max(page, end),
            )
        )

    logger.info("Detected %d sections in %s", len(sections), doc.file_name)
    return sections


def section_for_page(sections: list[Section], page: int) -> Section | None:
    for s in sections:
        if s.start_page <= page <= s.end_page:
            return s
    return None
