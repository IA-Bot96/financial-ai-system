# PyInstaller spec — one-folder bundle of the FastAPI extraction backend.
#
#   Build (from backend/):  pyinstaller aifi-backend.spec --noconfirm
#   Output:                 backend/dist/aifi-backend/   (exe + _internal/)
#
# Prefer the build_bundle.py wrapper — it runs this spec, then stages tesseract and the
# sentence-embedding model next to the exe so the bundle is fully offline.
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

# Non-Python assets the app loads via Path(__file__)-relative reads. Destinations mirror
# the package layout so those reads resolve inside the bundle.
datas = [
    ('app/engines/extraction/data/canonical_metric_registry.json', 'app/engines/extraction/data'),
    ('app/engines/extraction/prompts', 'app/engines/extraction/prompts'),
    ('app/engines/fie/qualitative_taxonomy.json', 'app/engines/fie'),
]
binaries = []

# uvicorn loads its protocol/loop backends dynamically — declare them explicitly.
hiddenimports = [
    'uvicorn.logging',
    'uvicorn.loops', 'uvicorn.loops.auto',
    'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan', 'uvicorn.lifespan.on',
]
# the whole app package (routes/engines are imported lazily in places)
hiddenimports += collect_submodules('app')

# Heavy third-party packages with data files / dynamic imports / native libs.
for pkg in (
    'sentence_transformers', 'transformers', 'tokenizers', 'huggingface_hub',
    'safetensors', 'torch', 'sklearn', 'scipy',
    'pdfplumber', 'pdfminer', 'fitz', 'PIL', 'pytesseract', 'regex',
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # noqa: BLE001 — a missing optional pkg shouldn't abort the build
        print(f'[aifi-backend.spec] skipping collect_all({pkg!r}): {exc}')

excludes = ['matplotlib', 'tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
            'IPython', 'notebook', 'pytest']

a = Analysis(
    ['run_server.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='aifi-backend',
    console=True,            # keep a console so backend logs are capturable
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,               # UPX + torch DLLs is fragile; keep off
    name='aifi-backend',
)
