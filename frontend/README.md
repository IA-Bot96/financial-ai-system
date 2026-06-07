# AI Financial Intelligence — Desktop (Electron + React)

Desktop UI over the Python FIE backend. Built with electron-vite + React + TypeScript +
Tailwind + Zustand. See `../docs/frontend-spec.md` and `../docs/frontend-build-phases.html`.

## Architecture (Phase 1)
- **main** spawns the FIE backend as a sidecar and exposes it to the renderer.
- **All backend HTTP is proxied through the main process** (`window.api.request`) — the
  sandboxed renderer makes no cross-origin calls, so **no CORS config is needed**.
- **renderer** = React app: a readiness-gated splash → app frame (left rail + Home).

## Prerequisites
- Node 18+ and npm.
- The Python backend deps installed (the repo's `backend/`).

## Run (dev)
```bash
cd frontend
npm install
```
Then choose how the backend starts:

**A. Point at an already-running backend (simplest):**
```bash
# terminal 1 — from repo root
cd backend && python -m uvicorn app.main:app --port 8000
# terminal 2
cd frontend && set FIE_BACKEND_URL=http://127.0.0.1:8000 && npm run dev   # Windows
# or: FIE_BACKEND_URL=http://127.0.0.1:8000 npm run dev                    # bash
```

**B. Let Electron spawn the backend:**
```bash
cd frontend
set FIE_BACKEND_DIR=..\backend        # path to the backend (cwd for uvicorn)
npm run dev                            # spawns: python -m uvicorn app.main:app --port <free>
```
Override the spawn command with `FIE_BACKEND_CMD` if your Python entrypoint differs.
The backend's stdout/stderr is written to `<userData>/backend.log` (shown on the error
screen if startup fails).

## Scripts
- `npm run dev` — launch the app with HMR.
- `npm run build` — type-check + bundle main/preload/renderer.
- `npm run typecheck` — TypeScript only (node + web projects).

## What works in Phase 1
- Splash → polls `/health` until the backend is reachable → app frame.
- Error screen with the backend log path + Retry if it never comes up.
- Left rail (Home · Sheet · PDF · Ask AI · Dashboard); Ask AI/Dashboard disabled until a
  workbook session exists. Centered `+` on Home (upload modal is Phase 2).
- Docked-panel placeholders (PDF left, Ask AI right) to validate the layout.

Packaging (PyInstaller backend + electron-builder) is Phase 8.
