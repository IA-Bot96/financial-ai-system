"""Tests for the logging format and per-document log files."""
import logging
import re

from app.core.logging import EngineFormatter, get_logger, per_document_log

_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| "
    r"(INFO|DEBUG|WARNING|ERROR|CRITICAL) \| [^|]+ \| .+$"
)


def test_formatter_shape():
    rec = logging.LogRecord("app.engines.extraction.pipeline.ingest", logging.INFO,
                            __file__, 1, "Starting OCR run", None, None)
    line = EngineFormatter().format(rec)
    assert _LINE.match(line)
    assert " | INFO | Ingest | Starting OCR run" in line   # module -> Layer column


def test_levels_render():
    for lvl, name in [(logging.WARNING, "WARNING"), (logging.ERROR, "ERROR")]:
        rec = logging.LogRecord("tables", lvl, __file__, 1, "details", None, None)
        line = EngineFormatter().format(rec)
        assert f" | {name} | Tables | details" in line


def test_per_document_log_creates_file(tmp_path):
    with per_document_log("millat-2025", log_dir=tmp_path, debug=False) as path:
        get_logger("app.engines.extraction.pipeline.tables").info("Detected 5 tables")
        get_logger("app.engines.extraction.pipeline.ingest").warning("OCR fallback used")

    assert path.exists()
    assert path.name.startswith("2") and path.name.endswith("_millat_2025.log")  # <ts>_<slug>.log
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines and all(_LINE.match(ln) for ln in lines)        # every line in format
    body = "\n".join(lines)
    assert "Starting extraction run" in body and "Finished extraction run" in body
    assert "| Tables | Detected 5 tables" in body
    assert "| WARNING | Ingest | OCR fallback used" in body


def test_separate_file_per_document(tmp_path):
    with per_document_log("a", log_dir=tmp_path) as p1:
        get_logger("tables").info("a-line")
    with per_document_log("b", log_dir=tmp_path) as p2:
        get_logger("tables").info("b-line")
    assert p1 != p2
    assert "a-line" in p1.read_text() and "a-line" not in p2.read_text()
