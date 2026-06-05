"""Logging foundation.

Console + per-document file logs in the format:
    2026-06-04 21:14:17.700 | INFO | Layer | message

The "Layer" column is derived from the logger's module name (e.g. the
`ingest` module logs as `Ingest`). Each processed PDF gets its own log file via
`per_document_log`.
"""
from __future__ import annotations

import logging
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)s | %(component)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# Third-party libraries that emit huge volumes of DEBUG (pdfminer alone can
# write hundreds of MB). Pin them to WARNING so they never reach any handler.
_NOISY_LIBRARIES = (
    "pdfminer", "pdfplumber", "httpx", "httpcore", "openai", "urllib3",
    "sentence_transformers", "PIL", "fontTools", "matplotlib",
)


def _silence_noisy_libraries() -> None:
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


# Apply at import so pdfminer is quiet even before configure_logging runs
# (e.g. when per_document_log is used without explicit logging setup).
_silence_noisy_libraries()


def _component_from_logger(name: str) -> str:
    """'app...pipeline.excel_writer' -> 'Excel Writer'."""
    seg = name.rsplit(".", 1)[-1] if name else "engine"
    return " ".join(w.capitalize() for w in seg.split("_")) or "Engine"


class EngineFormatter(logging.Formatter):
    """Adds the `component` column and keeps each record on a single line."""

    def __init__(self) -> None:
        super().__init__(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "component"):
            record.component = _component_from_logger(record.name)
        return super().format(record).replace("\n", "\\n")


def configure_logging(debug: bool = False) -> None:
    """Install a single console handler with the engine format (idempotent)."""
    level = logging.DEBUG if debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    # Drop existing handlers so re-config doesn't duplicate console lines.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(EngineFormatter())
    handler.setLevel(level)
    root.addHandler(handler)
    _silence_noisy_libraries()  # keep pdfminer/httpx/etc. quiet even in DEBUG


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug or "document"


def _unique_log_path(log_dir: Path, document_id: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = log_dir / f"{stamp}_{document_id}.log"
    if not base.exists():
        return base
    n = 2
    while (cand := log_dir / f"{stamp}_{document_id}_{n}.log").exists():
        n += 1
    return cand


@contextmanager
def per_document_log(document_id: str, log_dir: str | Path | None = None, debug: bool | None = None):
    """Capture all logs emitted within the block into a per-document file.

    Yields the log file path. The file handler is attached to the root logger
    for the duration, then removed — so each PDF produces its own log file while
    console logging continues unchanged.
    """
    from app.core.config import get_settings

    settings = get_settings()
    log_dir = Path(log_dir or settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_log_path(log_dir, _slugify(document_id))
    level = logging.DEBUG if (debug if debug is not None else settings.debug) else logging.INFO

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(EngineFormatter())
    handler.setLevel(level)

    root = logging.getLogger()
    prev_level = root.level
    if root.level == logging.NOTSET or level < root.level:
        root.setLevel(level)
    root.addHandler(handler)

    get_logger("run").info("Starting extraction run | document=%s | log=%s", document_id, path.name)
    try:
        yield path
    except Exception:
        get_logger("run").exception("Extraction run failed | document=%s", document_id)
        raise
    finally:
        get_logger("run").info("Finished extraction run | document=%s", document_id)
        root.removeHandler(handler)
        handler.close()
        root.setLevel(prev_level)
