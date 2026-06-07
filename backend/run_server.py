"""Self-contained server entry point (PyInstaller target).

Runs the FastAPI extraction backend under uvicorn. When the process is frozen into a
PyInstaller bundle it points tesseract (OCR), the local sentence-embedding model, and the
storage / log directories at the resources shipped beside the executable — so the packaged
desktop app needs no system Python, no separate tesseract install, and downloads no models.

Dev usage is unchanged: `python run_server.py --host 127.0.0.1 --port 8000`
(equivalent to the previous `uvicorn app.main:app …`).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _resource_dirs() -> list[Path]:
    """Where bundled resources may live: next to the exe (staged by build_bundle.py)
    and the PyInstaller extraction dir (_MEIPASS). Deduped, order = search priority."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
        mei = getattr(sys, "_MEIPASS", None)
        if mei:
            dirs.append(Path(mei))
    else:
        dirs.append(Path(__file__).resolve().parent)
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _first_existing(rel: str) -> Path | None:
    for base in _resource_dirs():
        p = base / rel
        if p.exists():
            return p
    return None


def _configure_bundle() -> None:
    """Point optional native/model dependencies at bundled copies (frozen builds only).
    Each is best-effort: a missing bundle just falls back to PATH / normal HF cache."""
    if not getattr(sys, "frozen", False):
        return

    # --- tesseract (OCR for scanned PDFs) ---
    tdir = _first_existing("tesseract")
    if tdir:
        exe = tdir / ("tesseract.exe" if os.name == "nt" else "tesseract")
        if exe.exists():
            # Settings.tesseract_cmd reads the TESSERACT_CMD env var (pydantic-settings).
            os.environ.setdefault("TESSERACT_CMD", str(exe))
        tessdata = tdir / "tessdata"
        if tessdata.is_dir():
            os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))

    # --- local sentence-embedding model (offline; no HuggingFace download) ---
    hf = _first_existing("hf")
    if hf:
        os.environ.setdefault("HF_HOME", str(hf))
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    # --- writable storage / logs (the desktop shell passes FIE_STORAGE_ROOT) ---
    storage = os.environ.get("FIE_STORAGE_ROOT")
    if storage:
        s = Path(storage)
        os.environ.setdefault("LOG_DIR", str(s / "logs"))
        os.environ.setdefault("FIE_TRACE_DIR", str(s / "logs" / "traces"))
        os.environ.setdefault("DEBUG_DUMP_DIR", str(s / "logs" / "debug"))


def _parse_addr() -> tuple[str, int]:
    host = os.environ.get("FIE_HOST", "127.0.0.1")
    port = int(os.environ.get("FIE_PORT", "8000"))
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]
        elif a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
    return host, port


def main() -> None:
    _configure_bundle()
    host, port = _parse_addr()
    import uvicorn

    from app.main import app

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
