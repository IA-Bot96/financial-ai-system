# Frontend Specification — AI Financial Intelligence (Desktop)

Status: **draft for build**. This is the blueprint for the Electron desktop UI over the
existing Python FIE backend. Decisions marked **[LOCKED]** are settled; **[DEFAULT]** are
my recommended choices for previously-open questions (override if needed); **[BACKEND]**
flags work required on the Python side before that screen can ship.

---

## 1. Goal & product shape

A single-window desktop app that feels like an Excel sheet. The user drops **PDFs**
(annual reports → OCR/extraction) or an **Excel** workbook (→ FIE ingestion), reviews and
edits the resulting financial data in an Excel-like grid, asks natural-language questions
with **cited** answers, and views a Power-BI-style dashboard — all over a locally-bundled
FIE backend.

Core principle: **one workbook per window = one source of truth**, shared live across the
Sheet, Ask AI, and Dashboard. Edits propagate everywhere on save.

---

## 2. Tech stack [LOCKED]

| Concern | Choice |
|---|---|
| Shell | Electron |
| Build | electron-vite + TypeScript |
| UI framework | React + TypeScript |
| Spreadsheet grid | **Univer** (Apache-2.0, in-process, formula engine, xlsx I/O) |
| Spreadsheet I/O | **SheetJS** (xlsx read/write where Univer's I/O is insufficient) |
| Charts | Apache **ECharts** |
| PDF viewer | **react-pdf** (pdfjs-dist) |
| Split panes | **react-resizable-panels** (or `allotment`) |
| State | **Zustand** |
| UI kit / styling | **shadcn/ui + Tailwind** |
| Markdown (chat) | **react-markdown** |
| HTTP | native `fetch` (to `http://127.0.0.1:<port>`) |

Not chosen / rejected: ONLYOFFICE (AGPL + needs Document Server — see docs/LICENSING.md),
Handsontable (non-commercial license), Redux (Zustand is enough).

---

## 3. Process architecture

```
Electron main  ──spawn──▶  Python FIE backend (PyInstaller exe)  ─ FastAPI @ 127.0.0.1:<port>
     │                                   ▲
     │ contextBridge (preload)           │ HTTP (localhost only)
     ▼                                   │
Electron renderer (React) ───────────────┘
```

- **main**: spawns the backend sidecar on a **free port chosen at launch**, passes the port
  to the renderer, kills the backend on `before-quit`. Owns native menu, file dialogs, and
  disk writes (`fs`).
- **preload**: exposes a typed `window.api` via `contextBridge` (no `nodeIntegration`,
  `contextIsolation: true`). Renderer never touches `fs`/`child_process` directly.
- **renderer**: React app. Talks to the backend over `fetch`; talks to the OS (open/save
  dialogs, disk writes) only through `window.api`.
- **Startup gate**: splash screen → poll **`GET /readiness`** until `200` → mount app. If
  the backend never becomes ready (or its boot **contract check** fails), show an error
  screen with the backend log path.

**CORS** [BACKEND]: add the Electron renderer origin (e.g. `app://.` or the dev
`http://localhost:5173`) to `CORS_ALLOW_ORIGINS`.

---

## 4. Backend contract

### 4.1 Existing endpoints (reuse as-is)
- `GET /health` · `GET /liveness` · `GET /readiness` · `GET /metrics`
- `POST /api/extraction/jobs` (multipart: `files[]` PDFs, optional `template`, `company`) → `{job_id, status}`
- `GET /api/extraction/jobs/{job_id}` → job `{status, progress, …}`
- `GET /api/extraction/jobs/{job_id}/download` → generated `.xlsx`

### 4.2 New endpoints required [BACKEND] — workbook sessions
The current `POST /api/fie/answer` resolves a workbook from a **hardcoded company map**.
The desktop opens **arbitrary** files, so we need a session model:

| Method & path | Body | Returns |
|---|---|---|
| `POST /api/fie/sessions` | multipart `file` (.xlsx) | `{session_id, company, years:[int], sheets:[{name, role, editable}], metrics:[str]}` |
| `POST /api/fie/sessions/{id}/answer` | `{query, audience?}` | the FIE `Response` (direct_answer, key_findings, citations, confidence, coverage, conflicts) |
| `POST /api/fie/sessions/{id}/reload` | multipart `file` (.xlsx) | re-ingest the edited workbook + **bust the cached store**; returns updated session metadata |
| `DELETE /api/fie/sessions/{id}` | — | drop the session/store |

- Sessions are in-memory (keyed by uuid); reuse `assert_safe_upload` on every upload.
- `reload` is called after **Save** so Ask AI / Dashboard reflect edits. It must invalidate
  the lru-cached store for that session.
- Peer comparison: a session may register peers later (out of scope for v1 — single workbook).

> **Ingestion runs once per load — never per query.** The session registry must hold the
> **store object** in memory (`{session_id: FinancialFactStore}`); `answer` **looks it up**
> and must **not** call `from_workbook` on the query path. `from_workbook` (~0.9 s parse)
> runs only on `POST /sessions` (create) and `POST /sessions/{id}/reload` (after save) — the
> query path reuses the resident store (~20 ms internal). This will be made explicit in the
> session-layer implementation so it can't regress. A 50-question chat = **1 ingest**, not 50.

### 4.3 Dashboard data [DEFAULT: client-side]
Dashboard charts are computed **client-side from the loaded workbook JSON** (instant,
always consistent with edits). No dashboard endpoint in v1. (If server-side aggregation is
wanted later, add `GET /api/fie/sessions/{id}/dashboard`.)

---

## 5. Global layout [LOCKED]

```
┌────┬──────────────────────────────────────────────────────────┐  ← top: SAVE BAR (when dirty)
│    │                                                            │     toasts: TOP-RIGHT
│ L  │  ┌──────────┬───────────────────────────┬──────────────┐ │
│ E  │  │   PDF    │        SHEET (Univer)      │   Ask AI     │ │
│ F  │  │ (left    │        — primary,          │  (right      │ │
│ T  │  │  dock,   │        always present —     │   dock,      │ │
│    │  │  resize) │        collapses width)     │   resize)    │ │
│ R  │  └──────────┴───────────────────────────┴──────────────┘ │
│ AIL│                                                            │
└────┴──────────────────────────────────────────────────────────┘
left rail: Home · Sheet · PDF · Ask AI · Dashboard · Save
```

- **PDF docks LEFT, Ask AI docks RIGHT — both resizable, both may be open at once** [LOCKED].
  Center sheet narrows to accommodate; it is never hidden or destroyed (scroll/edit state
  preserved). Min-widths enforced on all three panes.
- **[DEFAULT]** Below a min window width, an opening side panel becomes a temporary overlay
  **drawer** instead of crushing the sheet; or enforce a minimum window size.
- **Modals (upload + any) are centered** [LOCKED].
- **Save-changes bar: top, full width** [LOCKED]. **Info / error / warning: top-right toasts**
  [LOCKED].
- Left-rail items toggle their panel; Sheet is the default/home surface. Ask AI & Dashboard
  are disabled until a workbook is loaded.

---

## 6. Screens & flows

### 6.1 Landing
- Empty state with a centered **`+`** ("Load financial data"). Whole window is a drop target.
- Clicking `+` (or dropping files) opens the **Upload modal** (§6.2).
- Recent files: **out of scope for v1** [LOCKED] (no DB) — revisit via the userData state
  file (§9).

### 6.2 Upload modal (centered) [LOCKED]
One modal, two states:

**State A — choose:** drag-and-drop zone + header:
> **Load financial data** — *Drop PDFs to extract, or an Excel to analyze.*
Faint hint row: `📄 PDFs → extract   •   📊 Excel → analyze`.

**Routing rules** (auto-route by file type — no separate buttons):
| Dropped | Action |
|---|---|
| 1+ `.pdf` | OCR/extraction batch (§6.3) |
| exactly one `.xlsx`/`.xls` | FIE ingestion (§6.4) |
| multiple Excel | reject: "Open one workbook at a time" |
| mixed PDF + Excel | reject: "Load PDFs to extract, or an Excel to analyze — not both" |
| `.xlsm` / oversized / bad magic | reject with the backend reason (`assert_safe_upload`) |

**State B — progress:** the modal transitions in place to a per-file list: **name · size ·
progress bar · status**, plus overall progress, **Cancel**, and per-file **error + retry**.

### 6.3 PDF → OCR/extraction flow
1. Upload modal (State B) → `POST /api/extraction/jobs` (multipart PDFs) → poll
   `GET /api/extraction/jobs/{id}`.
2. Surface **OCR confidence**; flag low-confidence pages so review is targeted.
3. On complete → **Review** button. Clicking it:
   - closes the modal,
   - `GET …/download` the generated `.xlsx`, then `POST /api/fie/sessions` to ingest it,
   - loads the sheet, **enables Ask AI + Dashboard**, opens the **PDF panel (left)** for
     side-by-side review.
4. Cells are color-coded by **`validation_status`** (CLEAN / MISMATCH / WITHHELD) with a
   legend + toggle (§6.6).

### 6.4 Excel → ingestion flow
1. Upload modal (State B) shows the workbook being extracted (name · size · progress · status)
   → `POST /api/fie/sessions` (multipart xlsx).
2. On complete → modal closes, sheet loads with data; **Ask AI + Dashboard enabled**.

### 6.5 Sheet (Univer) — center, primary
- Excel-like grid: sheet tabs, formulas, freeze panes, edit.
- **Multi-sheet handling [DEFAULT]:** financial sheets (P&L, BS, CF, equity) are **editable**;
  meta-sheets (`source_ledger`, `validation_ledger`, insights) are **hidden behind a
  "Show source sheets" toggle and read-only**.
- **Edit → dirty:** first edit shows the **top Save bar** ("You have unsaved changes ·
  Save · Discard"). Edited cells are visually diff-highlighted.
- **Undo/redo** (Univer built-in + Ctrl+Z/Y).
- **Number formatting** per §10.

### 6.6 Validation overlay [DEFAULT]
- Cell background tint by `validation_status`: CLEAN (none), MISMATCH (amber), WITHHELD (grey/strike).
- Legend chip + a toggle to show/hide the overlay. Sourced from the backend's
  `data_quality_flags` / per-fact status. Dev signal only — never blocks editing.

### 6.7 PDF panel — LEFT dock, resizable [LOCKED]
- `react-pdf` viewer of the source report(s); page nav, zoom.
- Target for **insight citations**: clicking an Ask-AI insight `[Cn]` jumps to its page.
- Optional later: click a sheet cell → jump to its `source_ref` page.

### 6.8 Ask AI panel — RIGHT dock, resizable [LOCKED]
The payoff screen — render the **structured** `Response`, not raw text:
- Chat thread; input box; **suggested-question chips**.
- Each answer renders: **direct answer**, **key findings** with inline **citation chips
  `[Cn]`**, a **confidence badge** (High/Medium/Low), **coverage caveats** (degraded,
  insufficient-evidence, suppressed ratios) as small inline banners, and **surfaced
  conflicts/divergence** (shown, not auto-resolved — mirror the engine).
- **Citation chip click routes by kind** [DEFAULT]:
  - `financial` → scroll + highlight the **cell** in the Sheet (center),
  - `insight` → jump to the **PDF page** (left panel),
  - `external` → open the **article link** (system browser).
- Calls `POST /api/fie/sessions/{id}/answer`. Spinner while pending (internal answers are
  fast ~20 ms; news/valuation add network latency). Graceful "insufficient citable
  evidence" state.

### 6.9 Dashboard
- Power-BI-style. **KPI card row** on top (Revenue, PAT, margins, YoY), ratio /
  balance-sheet / working-capital charts below in a responsive ECharts grid.
- **Two multi-select filters** [LOCKED]: **Graph type** (key ratios, balance sheet, working
  capital, …) and **Year** (derived from the workbook's years) + **Apply** / **Reset**
  (explicit apply, not live) [LOCKED].
- Data computed **client-side from the loaded workbook** (§4.3) so it always matches edits.
- **Export dashboard to PNG/PDF**; light/dark theme.

### 6.10 Save
- **Save bar (top)** appears whenever dirty.
- **[DEFAULT] Save semantics by path:**
  - **OCR path** (new data, no original): **Save As** → Electron dialog → write **`.xlsx`
    + the extracted `.json`** side by side.
  - **Excel path** (edited existing file): **Save** overwrites the original; **Save As** for
    a copy. JSON travels alongside the xlsx.
- On Save: (1) renderer produces xlsx bytes (Univer/SheetJS) → `window.api` writes to disk;
  (2) `POST /api/fie/sessions/{id}/reload` with the same bytes so the backend re-ingests and
  busts its cache → Ask AI/Dashboard now reflect edits; (3) clear dirty + toast "Saved".

---

## 7. State model (Zustand)

```ts
interface AppState {
  backend: { port: number; status: "starting"|"ready"|"error" };
  session: { id: string; company: string; years: number[];
             sheets: SheetMeta[]; metrics: string[] } | null;
  workbook: { data: WorkbookJSON; dirty: boolean; editedCells: CellRef[];
              filePath: string | null; origin: "ocr"|"excel" };
  panels: { pdf: boolean; askAI: boolean; splitLeft: number; splitRight: number };
  view: "home"|"sheet"|"dashboard";
  chat: { messages: ChatTurn[]; pending: boolean };
  jobs: ExtractionJob[];        // OCR/ingestion progress
  toasts: Toast[];
}
```
- Single store; Sheet, Ask AI, Dashboard all read `workbook`/`session`.
- `dirty` drives the top save bar and the close/open guards (§8).

---

## 8. Unsaved-changes guard [LOCKED scope]
Intercept and confirm when `workbook.dirty`:
- **window close** (Electron `before-quit`),
- **opening a new file** (the `+` / drop),
- navigating away in a way that would drop edits.
Confirm dialog: *Save · Discard · Cancel*.

## 9. Persistence without a DB [DEFAULT]
- Write a small **`state.json` to Electron `userData`**: `{lastFilePath, autosaveDraftPath,
  windowBounds}`. Enables **crash recovery** (reopen the autosaved draft) and later seeds
  Recent files. Not a database.

## 10. Formatting convention [DEFAULT]
One shared formatter used by Sheet + Dashboard + Ask AI:
- "Rupees in thousand" base scale; thousands separators; **negatives in parentheses**;
  ratios as `x` (e.g. `1.42x`), percentages `1 dp`. Currency label shown once per context.

## 11. Notifications [LOCKED]
- **Save bar (top, full width):** persistent dirty-state + Save/Discard.
- **Toasts (top-right):** transient info / warning / error (save success, OCR failure, network
  off, backend reconnecting). Auto-dismiss; errors stay until dismissed.

## 12. State matrix (every async needs these)
loading · empty · error · degraded. Specifically: backend not ready; OCR file failed (retry);
ingestion failed; Ask AI degraded / insufficient evidence; dashboard with no data; news
offline (internal Q&A still works).

## 13. Keyboard & native menu
- **Ctrl+S** Save, **Ctrl+Z/Y** undo/redo, **Ctrl+O** open.
- Native menu: File (Open, Save, Save As, Exit) · Edit (Undo/Redo) · View (toggle PDF / Ask
  AI / Dashboard, theme) · Help.

## 14. Packaging / distribution
- **electron-builder** (Windows target first).
- Backend bundled via **PyInstaller**, launched as the sidecar (§3).
- First run must need **no Docker / no external services**.
- Code-signing + auto-update: **later**.

---

## 15. Build order (phases)
1. **Skeleton** — electron-vite + React/TS; main spawns Python sidecar on a free port;
   readiness-gated splash; preload `window.api`; left rail + empty Home.
2. **Sheet + open** — Univer + SheetJS; landing `+` → upload modal (choose→progress);
   Excel path → `POST /sessions` → load sheet; multi-sheet + dirty/save bar.
3. **OCR path** — extraction jobs modal (per-file progress + retry) → download → ingest →
   validation-status overlay.
4. **Ask AI** — right dock; structured Response rendering; citation chips + cross-panel nav;
   confidence/coverage/conflicts. (Highest user value.)
5. **PDF panel** — left dock; side-by-side; insight-citation jump.
6. **Dashboard** — ECharts; workbook-derived filters; KPI cards; export.
7. **Save + recovery + polish** — save semantics per path, reload/cache-bust, userData
   state file, toasts/guards, keyboard/menu, packaging.

## 16. Out of scope (v1)
Recent files (no DB), auth / multi-user, real-time collaboration, multiple workbooks per
window, server-side dashboard aggregation, auto-update, code-signing.

---

## 17. Open decisions to confirm (defaults applied above)
1. **[BACKEND] session/ingest endpoints + cache-bust on reload** — required before Sheet/Ask
   AI/Dashboard can use arbitrary user files. *(biggest item)*
2. Dashboard data: **client-side** [DEFAULT] vs server endpoint.
3. Save semantics: OCR=Save As, Excel=overwrite [DEFAULT].
4. Crash recovery via userData `state.json` [DEFAULT].
5. Multi-sheet: meta-sheets hidden+read-only behind a toggle [DEFAULT].
6. Both side panels may coexist (3-column) with min-widths; drawer fallback on narrow
   windows [DEFAULT].
