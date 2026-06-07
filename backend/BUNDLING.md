# Bundling the backend into the desktop app

The Electron app can ship a **self-contained** copy of this FastAPI backend — no system
Python, no separate tesseract install, no model downloads at runtime. The backend is frozen
with PyInstaller into a one-folder bundle and copied into the installer via electron-builder.

## How it fits together

```
backend/run_server.py        PyInstaller entry; wires bundled tesseract/model/storage when frozen
backend/aifi-backend.spec    PyInstaller spec (datas, hidden imports, collect_all for torch etc.)
backend/build_bundle.py      runs PyInstaller + stages tesseract + the embedding model
        └─ dist/aifi-backend/ {aifi-backend.exe, _internal/, tesseract/, hf/}
frontend  electron-builder `extraResources`  copies dist/aifi-backend → resources/backend
frontend  src/electron/main/backend.ts       launches resources/backend/aifi-backend(.exe) when packaged
```

In dev nothing changes: `backend.ts` still runs `python run_server.py` from `backend/`
(or an already-running server via `FIE_BACKEND_URL`).

## Build steps (run on the target OS — a bundle is OS-specific)

Prereqs, once:

```bash
pip install pyinstaller
# pre-cache the embedding model so build_bundle can stage it offline:
python -c "from sentence_transformers import SentenceTransformer as T; T('sentence-transformers/all-MiniLM-L6-v2')"
# tesseract must be installed and on PATH (build_bundle copies it into the bundle)
```

Then, from `frontend/`:

```bash
npm run dist:full      # = icons + backend:bundle + dist  (full installer)
```

or step by step:

```bash
npm run backend:bundle # -> backend/dist/aifi-backend/   (PyInstaller + tesseract + model)
npm run dist           # -> frontend/release/AI Financial Intelligence Setup <ver>.exe
```

`pack:dir` (`npm run pack:dir`) builds the unpacked app without the installer — fastest way
to smoke-test that the bundled backend launches.

## Runtime wiring (frozen builds)

`run_server.py` sets, only when `sys.frozen`:

- `TESSERACT_CMD` / `TESSDATA_PREFIX` → `resources/backend/tesseract`
- `HF_HOME` + `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` → `resources/backend/hf`
- `LOG_DIR` / `FIE_TRACE_DIR` / `DEBUG_DUMP_DIR` → under `FIE_STORAGE_ROOT`

`backend.ts` passes `FIE_STORAGE_ROOT = <userData>/backend-storage` and the chosen port.
`config.py` resolves `BACKEND_ROOT` to the exe dir when frozen and honours `FIE_STORAGE_ROOT`.

## Caveats

- **OpenAI API key.** Extraction calls GPT, so the installed app still needs an
  `OPENAI_API_KEY` in the environment (passed through to the backend). A bundled secret is
  not appropriate; provide it via the OS environment or a future in-app settings field.
- **Size.** Bundling PyTorch (via sentence-transformers) makes the backend ~1.5–3 GB. To
  shrink it, drop the embedding model — the extractor degrades gracefully (`get_embedder`
  returns `None`) but classification/metric-resolution quality falls; remove `torch`/
  `sentence_transformers` from the spec's `collect_all` loop if you accept that trade-off.
- **Per-OS builds.** PyInstaller bundles are not cross-platform; build on each target OS.
- **Code signing.** Unsigned installers trigger SmartScreen on Windows; add a cert for
  public distribution.
