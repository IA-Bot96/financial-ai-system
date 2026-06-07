"""Build a fully self-contained backend bundle.

Steps:
  1. Run PyInstaller against aifi-backend.spec  -> dist/aifi-backend/ (exe + _internal/)
  2. Stage the tesseract OCR binary + tessdata  -> dist/aifi-backend/tesseract/
  3. Stage the local sentence-embedding model   -> dist/aifi-backend/hf/

Run from the backend/ directory:  python build_bundle.py
Requires: pip install pyinstaller   (plus the app's own runtime deps already installed).

The Electron app (electron-builder `extraResources`) copies dist/aifi-backend/ into the
packaged app's resources/backend/, and run_server.py wires these staged dirs at startup.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "aifi-backend"
EMBED_MODEL = "all-MiniLM-L6-v2"  # sentence-transformers model used by the extractor


def run_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("PyInstaller is not installed. Run: pip install pyinstaller")
    print("== PyInstaller ==")
    subprocess.check_call(
        [sys.executable, "-m", "PyInstaller", "aifi-backend.spec", "--noconfirm", "--clean"],
        cwd=str(ROOT),
    )
    if not DIST.exists():
        sys.exit(f"PyInstaller did not produce {DIST}")


def stage_tesseract() -> None:
    print("== Staging tesseract ==")
    exe = shutil.which("tesseract")
    if not exe:
        print("  ! tesseract not found on PATH — OCR of scanned PDFs will be unavailable "
              "in the bundle. Install it and re-run, or accept text-PDF-only.")
        return
    src_dir = Path(exe).resolve().parent
    dst_dir = DIST / "tesseract"
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    # copy the whole install dir (exe + DLLs); then make sure tessdata is present
    shutil.copytree(src_dir, dst_dir)
    if not (dst_dir / "tessdata").is_dir():
        tessdata = os.environ.get("TESSDATA_PREFIX")
        cand = Path(tessdata) if tessdata else (src_dir / "tessdata")
        if cand.is_dir():
            shutil.copytree(cand, dst_dir / "tessdata")
        else:
            print(f"  ! tessdata not found ({cand}); OCR languages may be missing.")
    print(f"  staged tesseract from {src_dir}")


def stage_embedding_model() -> None:
    print("== Staging embedding model ==")
    # Locate the model in the local HuggingFace hub cache.
    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    hub = hf_home / "hub"
    matches = list(hub.glob(f"models--*{EMBED_MODEL}*")) if hub.is_dir() else []
    if not matches:
        print(f"  ! {EMBED_MODEL} not found under {hub}. Pre-download it once with:")
        print('    python -c "from sentence_transformers import SentenceTransformer as T; '
              f'T(\'sentence-transformers/{EMBED_MODEL}\')"')
        print("  then re-run. The bundle will otherwise try (and fail) to download offline.")
        return
    dst_hub = DIST / "hf" / "hub"
    dst_hub.mkdir(parents=True, exist_ok=True)
    for m in matches:
        target = dst_hub / m.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(m, target, symlinks=False)
        print(f"  staged model {m.name}")


def main() -> None:
    run_pyinstaller()
    stage_tesseract()
    stage_embedding_model()
    size_mb = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file()) / 1e6
    print(f"\n✓ bundle ready: {DIST}  (~{size_mb:.0f} MB)")
    print("  Electron picks this up via extraResources on `npm run dist`.")


if __name__ == "__main__":
    main()
