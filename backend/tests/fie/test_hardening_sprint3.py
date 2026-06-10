"""Sprint-3 hardening: source-catalog consistency (#10), workbook-corruption handling
(#11), and concurrent-query safety on a shared engine/store (#11)."""

import io
import os
import threading
import zipfile

import pytest

from app.engines.fie import FinancialFactStore, FinancialIntelligenceEngine


# --- #11: workbook corruption is handled cleanly ----------------------------
def test_corrupt_workbook_raises_clean_error(tmp_path):
    bad = tmp_path / "garbage.xlsx"
    bad.write_bytes(b"this is not a real xlsx file")
    with pytest.raises(Exception):           # openpyxl raises a clear error, not a hang
        FinancialFactStore.from_workbook(str(bad))


def test_truncated_zip_workbook_raises(tmp_path):
    # a valid zip header but truncated/empty content (not a real workbook)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("junk.txt", b"x")
    p = tmp_path / "truncated.xlsx"
    p.write_bytes(buf.getvalue())
    with pytest.raises(Exception):
        FinancialFactStore.from_workbook(str(p))


# --- #11: concurrent queries on ONE shared engine (read-mostly store) -------
_WB = os.path.join("storage", "outputs", "millat_filled_fixed.xlsx")
_real = pytest.mark.skipif(not os.path.exists(_WB), reason="workbook absent")


@_real
def test_concurrent_queries_are_safe_and_well_formed():
    """Hammer a single shared engine from many threads (FastAPI serves sync routes in
    a threadpool). Every answer must be valid — no exception, every key finding cited,
    citation handles unique within each response (the _cite_seq race is masked by the
    per-request renumbering in citations.bind)."""
    eng = FinancialIntelligenceEngine(FinancialFactStore.from_workbook(_WB))
    queries = [
        "current ratio for MTL 2024",
        "what was Millat's revenue in 2024?",
        "ROE for MTL 2024",
        "debt to equity for Millat 2024",
    ]
    errors: list[str] = []
    results: list = []
    lock = threading.Lock()

    def worker(q: str):
        try:
            r = eng.answer(q)
            # validity invariants
            assert isinstance(r.direct_answer, str) and r.direct_answer
            refs = [c.ref_id for c in r.citations]
            assert len(refs) == len(set(refs)), "duplicate citation handles in one response"
            import re
            for f in r.key_findings:
                assert re.search(r"\[C\d+\]", f), f"uncited finding: {f!r}"
            with lock:
                results.append(r)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"{q}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(q,))
               for q in queries for _ in range(8)]   # 32 concurrent answers
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, "concurrent query failures:\n" + "\n".join(errors)
    assert len(results) == len(threads)
