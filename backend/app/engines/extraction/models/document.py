"""Data models produced by the Ingest / OCR layer."""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PageKind(str, Enum):
    """How a page's text was obtained."""

    native = "native"  # text extracted directly from the PDF text layer
    ocr = "ocr"        # page was image-only/scanned and OCR'd
    empty = "empty"    # no usable text found by either method


class PageText(BaseModel):
    page: int = Field(..., description="1-based page number")
    text: str = Field("", description="Extracted text for the page")
    kind: PageKind = PageKind.native
    char_count: int = Field(0, description="Number of characters in `text`")
    ocr_confidence: Optional[float] = Field(
        None, description="Mean OCR word confidence (0-100), if OCR was used"
    )
    # OCR word boxes ({text,x0,x1,top,bottom}) captured during ingest, reused by
    # table detection so scanned pages are OCR'd only once.
    ocr_words: list[dict] = Field(default_factory=list, exclude=True, repr=False)


class IngestedDoc(BaseModel):
    file_name: str
    page_count: int = 0
    company: Optional[str] = Field(None, description="Company name extracted from the document")
    report_year: Optional[int] = Field(None, description="Fiscal year extracted from the document")
    is_scanned: bool = Field(False, description="True if most pages required OCR")
    pages: list[PageText] = Field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for p in self.pages if p.kind == PageKind.ocr)
