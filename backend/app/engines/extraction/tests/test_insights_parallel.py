"""extract_insights runs its batch GPT calls concurrently, preserving order,
provenance filtering, and per-batch error isolation."""
import re

from app.core.config import get_settings
from app.engines.extraction.models.document import IngestedDoc, PageKind, PageText
from app.engines.extraction.models.insight import Insight, InsightList
from app.engines.extraction.pipeline.insights import extract_insights

_PROSE = (
    "Export volumes rose sharply supported by strong global demand while the "
    "company expanded capacity and managed working capital, debt and margins "
    "prudently across all of its key export markets during the year under review."
)


def _doc():
    doc = IngestedDoc(file_name="r.pdf", report_year=2025)
    doc.pages = [
        PageText(page=1, text=f"CHAIRMAN'S REVIEW\n{_PROSE}", kind=PageKind.native, char_count=200),
        PageText(page=2, text=f"CEO'S MESSAGE\n{_PROSE}", kind=PageKind.native, char_count=200),
        PageText(page=3, text=f"BUSINESS REVIEW\n{_PROSE}", kind=PageKind.native, char_count=200),
    ]
    doc.page_count = 3
    return doc


class _StubGPT:
    """Echoes one insight per batch, citing the chunk's own (page, section) so it passes
    the provenance filter. Raises for `fail_page` to exercise error isolation."""

    def __init__(self, fail_page=None):
        self.fail_page = fail_page
        self.calls = 0

    def complete_structured(self, system, user, schema, images=None):
        self.calls += 1
        page = int(re.search(r"page_number:\s*(\d+)", user).group(1))
        section = re.search(r"source_section:\s*(.+)", user).group(1).strip()
        if page == self.fail_page:
            raise RuntimeError("simulated batch failure")
        # Section names differ (Chairman/CEO/Business) -> distinct, not deduped together.
        return InsightList(insights=[
            Insight(area="A", takeaway=f"{section}: distinct finding {page}", source_section=section,
                    page=page, confidence=0.9)])


def _one_batch_per_chunk(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "insights_chunks_per_call", 1)   # 1 chunk/batch -> several batches
    monkeypatch.setattr(s, "insights_max_chunks", None)
    return s


def test_serial_and_parallel_agree(monkeypatch):
    # Parallelizing the batch calls must not change the result (vs the old serial loop).
    s = _one_batch_per_chunk(monkeypatch)
    monkeypatch.setattr(s, "insights_workers", 1)           # serial path
    serial = extract_insights(_doc(), _StubGPT())[0]

    monkeypatch.setattr(s, "insights_workers", 8)           # parallel path
    parallel = extract_insights(_doc(), _StubGPT())[0]

    assert len(serial) >= 2                                 # multiple batches actually exercised
    assert sorted(i.page for i in serial) == sorted(i.page for i in parallel)
    assert all(i.source_report_year == 2025 for i in parallel)


def test_parallel_isolates_a_failed_batch(monkeypatch):
    s = _one_batch_per_chunk(monkeypatch)
    monkeypatch.setattr(s, "insights_workers", 4)           # parallel path

    base = _StubGPT()
    baseline = sorted(i.page for i in extract_insights(_doc(), base)[0])
    assert base.calls >= 2 and len(baseline) >= 2          # >=2 batches to isolate among

    victim = baseline[0]
    failing = _StubGPT(fail_page=victim)                    # one batch raises
    survived = sorted(i.page for i in extract_insights(_doc(), failing)[0])

    assert failing.calls == base.calls                      # every batch still attempted
    assert victim not in survived                           # the failed batch dropped...
    assert survived == [p for p in baseline if p != victim] # ...others unaffected
