import { create } from 'zustand'
import type { ParsedSheet } from '@/lib/sheetjs'
import { api, type Citation, type FieResponse } from '@/api'
import { parseWorkbook, readSheetSources } from '@/lib/sheetjs'
import { buildEditedXlsx, pendingEditsForQuery } from '@/lib/save'
import { nowLocalIso } from '@/lib/history'
import {
  buildValidationData,
  colorOf,
  VALIDATION_BG,
  type ValidationData,
  type ValidationIssue
} from '@/lib/validation'
import { writeCell, setCellBackground } from '@/lib/sheetApi'

// Insights worksheet column order (excel_writer.INSIGHT_COLUMNS), 0-based:
// 0 Year | 1 Source Report Year | 2 Area | 3 Takeaway | 4 Source Section | 5 Page | 6 Confidence
const INS_AREA = 2
const INS_YEAR = 0
const INS_PAGE = 5
const INS_SECTION = 4

/**
 * Locate the row for an insight citation on the workbook's "Insights" sheet so the chip
 * opens the cell in the grid instead of the source PDF. The citation locator carries
 * area/year/page/source_section but not the row; we match those against the sheet (area is
 * required, year/page/section break ties between insights sharing an area). Returns an A1
 * target on the Area column, or — if no row matches — the first insight sheet at A1, or
 * null when no insight sheet is loaded (caller then falls back to the PDF).
 */
function findInsightCell(
  sheets: ParsedSheet[],
  loc: Record<string, unknown>
): { sheet: string; cell: string } | null {
  const insightSheets = sheets
    .filter((s) => /insight/i.test(s.name))
    .sort((a, b) => Number(b.name.toLowerCase() === 'insights') - Number(a.name.toLowerCase() === 'insights'))
  if (!insightSheets.length) return null

  const norm = (v: unknown) => String(v ?? '').trim().toLowerCase()
  const area = norm(loc.area)

  for (const sheet of insightSheets) {
    let bestRow = -1
    let bestScore = 0
    for (const rStr in sheet.cellData) {
      const r = Number(rStr)
      if (r === 0) continue // header row
      const row = sheet.cellData[r]
      if (!area || norm(row?.[INS_AREA]?.v) !== area) continue // area must match
      let score = 1
      if (loc.year != null && row?.[INS_YEAR]?.v != null && Number(row[INS_YEAR].v) === Number(loc.year)) score++
      if (loc.page != null && row?.[INS_PAGE]?.v != null && Number(row[INS_PAGE].v) === Number(loc.page)) score++
      if (loc.source_section && norm(row?.[INS_SECTION]?.v) === norm(loc.source_section)) score++
      if (score > bestScore) {
        bestScore = score
        bestRow = r
      }
    }
    if (bestRow >= 0) return { sheet: sheet.name, cell: `C${bestRow + 1}` } // C = Area; +1 → 1-based row
  }
  return { sheet: insightSheets[0].name, cell: 'A1' } // area not found — at least open the sheet
}

export type View = 'home' | 'sheet' | 'dashboard'
export interface ChatTurn {
  id: string
  role: 'user' | 'assistant'
  text?: string
  response?: FieResponse
  frame?: Record<string, unknown>  // resolved QueryFrame echoed from the engine response
  error?: string
  timestamp: number
}
export type Toast = { id: string; kind: 'info' | 'warning' | 'error' | 'success'; text: string }

/** Session metadata returned by POST /api/fie/sessions (Phase 0 backend). */
export interface SessionMeta {
  session_id: string
  company: string
  years: number[]
  sheets: { name: string; role: string; editable: boolean }[]
  metrics: string[]
}

export interface ValidationSummary {
  production_ready?: boolean | null
  fully_reconciled?: boolean | null
  validation_failures?: number | null
  detail_incomplete?: number | null
  withheld?: number | null
  quarantined?: number | null
}

/** One provenance entry for a worksheet: which PDF + pages the sheet's data came from. */
export interface SheetSourceEntry {
  report_file: string // original uploaded PDF filename (e.g. "2024.pdf")
  pages: number[]      // 1-based PDF page numbers
  table_ids?: string[]
  weight: number       // higher = stronger primary source
}
/** `sheet_sources` from the extraction job: worksheet name → entries (highest weight first). */
export type SheetSources = Record<string, SheetSourceEntry[]>

/**
 * Choose which source entry to sync the PDF to for a sheet. Prefers the PDF the user
 * currently has open (so switching sheets keeps you in the same document when that
 * document is one of the sheet's sources); otherwise falls back to the primary
 * (highest-weight) entry. Returns null when the sheet has no lineage.
 */
export function pickSourceEntry(
  entries: SheetSourceEntry[] | undefined,
  preferredFile: string | null
): SheetSourceEntry | null {
  if (!entries || !entries.length) return null
  if (preferredFile) {
    const match = entries.find(
      (e) => e.report_file.toLowerCase() === preferredFile.toLowerCase()
    )
    if (match) return match
  }
  return entries[0]
}

/**
 * Reconstruct `sheet_sources` from the "Source Ledger" sheet embedded in an extracted
 * workbook. The extraction emits sheet→PDF page lineage to the job API (used right after
 * extraction) but does NOT persist it in the xlsx — except indirectly via the Source Ledger
 * sheet, which lists every written cell's origin (Sheet | … | Report file | Page | …). So
 * when the user re-opens a saved extracted workbook (and attaches its source PDFs), we
 * rebuild the same map client-side. Returns {} when there's no parseable ledger.
 */
export function reconstructSheetSources(sheets: ParsedSheet[]): SheetSources {
  const ledger = sheets.find((s) => s.name.trim().toLowerCase() === 'source ledger')
  if (!ledger) return {}
  const header = ledger.cellData[0]
  if (!header) return {}

  const colOf = (label: string): number | null => {
    for (const [c, cell] of Object.entries(header)) {
      const v = cell?.v
      if (typeof v === 'string' && v.trim().toLowerCase() === label) return Number(c)
    }
    return null
  }
  const cSheet = colOf('sheet')
  const cFile = colOf('report file')
  const cPage = colOf('page')
  if (cSheet == null || cFile == null || cPage == null) return {}

  // accumulate: sheet → report_file → { pages, weight (# contributing cells) }
  const acc: Record<string, Record<string, { pages: Set<number>; weight: number }>> = {}
  for (const [rowKey, row] of Object.entries(ledger.cellData)) {
    if (Number(rowKey) === 0) continue // header row
    const sheet = row?.[cSheet]?.v
    const file = row?.[cFile]?.v
    if (typeof sheet !== 'string' || typeof file !== 'string' || !sheet || !file) continue
    const rawPage = row?.[cPage]?.v
    const page = typeof rawPage === 'number' ? rawPage : Number(rawPage)
    const ent = ((acc[sheet] ||= {})[file] ||= { pages: new Set<number>(), weight: 0 })
    if (Number.isFinite(page) && page >= 1) ent.pages.add(page)
    ent.weight += 1
  }

  const out: SheetSources = {}
  for (const [sheet, byFile] of Object.entries(acc)) {
    const entries: SheetSourceEntry[] = Object.entries(byFile)
      .map(([report_file, e]) => ({
        report_file,
        pages: [...e.pages].sort((a, b) => a - b),
        weight: e.weight
      }))
      .filter((e) => e.pages.length) // an entry with no page can't drive navigation
      .sort((a, b) => b.weight - a.weight || a.report_file.localeCompare(b.report_file))
    if (entries.length) out[sheet] = entries
  }
  return out
}

/** The source pages of a given PDF for a sheet (e.g. BS → 2025.pdf → [137,138,139,179]).
 *  Used to scope the PDF value-highlight search to a sheet's source pages, not just one. */
function sourcePagesFor(
  sheetSources: SheetSources,
  sheetName: string,
  file: string | null
): number[] {
  if (!file) return []
  const pages = new Set<number>()
  for (const e of sheetSources[sheetName] ?? []) {
    if (e.report_file.toLowerCase() === file.toLowerCase()) e.pages.forEach((p) => pages.add(p))
  }
  return [...pages].sort((a, b) => a - b)
}

/** Highest 4-digit year (19xx/20xx) in a path's filename, or -Infinity if none. */
function pdfYear(path: string): number {
  const name = path.replace(/\\/g, '/').split('/').pop() ?? ''
  const years = name.match(/(?:19|20)\d{2}/g)
  return years ? Math.max(...years.map(Number)) : -Infinity
}

/** Filename of the latest-year PDF among the given paths (falls back to the first). */
function latestPdfFile(paths: string[]): string | null {
  if (!paths.length) return null
  let best = paths[0]
  for (const p of paths) if (pdfYear(p) > pdfYear(best)) best = p
  return best.replace(/\\/g, '/').split('/').pop() ?? null
}

/** Read a cell's value (as a string) from a parsed sheet by A1 reference, e.g. "C30". */
function cellValueAt(sheets: ParsedSheet[], sheetName: string, a1: string): string | null {
  const m = /^([A-Za-z]+)(\d+)$/.exec(a1.trim())
  if (!m) return null
  let c = 0
  for (const ch of m[1].toUpperCase()) c = c * 26 + (ch.charCodeAt(0) - 64)
  const r = Number(m[2]) - 1
  const sheet = sheets.find((s) => s.name === sheetName)
  const v = sheet?.cellData[r]?.[c - 1]?.v
  return v == null || v === '' ? null : String(v)
}

interface AppState {
  backend: { status: 'starting' | 'ready' | 'error'; logPath: string }
  session: SessionMeta | null
  sheets: ParsedSheet[]
  loadSeq: number // bumped on every explicit (re)load so the grid remounts cleanly
  cleanToken: number // bumped on a successful save so the grid re-baselines its undo depth
  showSource: boolean
  workbook: { dirty: boolean; filePath: string | null; origin: 'ocr' | 'excel' | null }
  pdfPaths: string[]
  validation: ValidationSummary | null
  sheetSources: SheetSources       // worksheet → source PDF pages (from extraction)
  activeSheet: string | null       // currently selected worksheet tab name
  activePdf: string | null         // filename of the PDF currently shown in the viewer
  activePdfPage: number | null     // 1-based page currently in view (reported by PdfPanel)
  syncPdfToSheet: boolean          // auto-jump the PDF to the active sheet's source page
  panels: { pdf: boolean; askAI: boolean }
  panelWidth: { pdf: number; askAI: number }
  nav: {
    cell: { sheet: string; cell: string } | null
    pdfFile: string | null         // target PDF (by filename); null = keep current
    pdfPage: number | null
    pdfQuery: string | null        // term to highlight on the PDF page (cell/citation value)
    pdfQueryPage: number | null    // preferred page (null = the page currently in view)
    pdfQueryPages: number[]        // source pages of this PDF to search (sheet lineage)
    // Separate trigger counters so grid-cell navigation, PDF navigation, and PDF highlight
    // don't cross-fire. (PDF-sync on a sheet activation bumps pdfSeq only; otherwise it would
    // re-trigger the cell-nav effect, whose activate() fires the sheet-change → PDF-sync loop.
    // pdfQuerySeq is bumped only by citation/cell-select so plain navigation never re-highlights.)
    cellSeq: number
    pdfSeq: number
    pdfQuerySeq: number
  }
  chat: { messages: ChatTurn[]; pending: boolean }
  view: View
  uploadOpen: boolean
  attachPdfsOpen: boolean // "Attach PDFs" modal (view PDFs alongside a workbook with no source)
  settingsOpen: boolean   // Settings page (engine config) overlay
  confirmDiscard: boolean // "Discard Changes?" prompt before navigating to upload (New)
  // read-only validation overlay derived from the workbook's "Validation Ledger" sheet
  validationLedger: ValidationData | null // null = no ledger present (feature inactive)
  validationEnabled: boolean               // master on/off for the review feature (Settings, persisted)
  showValidation: boolean                  // bar toggle: highlight cells on/off (within the feature)
  validationPanelOpen: boolean             // the issues side panel is open
  workbookNotesDismissed: boolean          // user dismissed the "whole workbook" notes group (session)
  // session "Manually Verified" cell writes (ledger "sheet!A1" → "TRUE"|""), replayed after a
  // highlight-toggle remount so the unsaved edits survive; cleared on a successful save.
  verifyWrites: Record<string, string>
  // change-history (edit log): when the workbook was opened this session (-> "(session)"
  // marker + "this session" scoping) and per-cell last-edit times ("sheet!A1" -> ISO).
  sessionStart: string | null
  editTimes: Record<string, string>
  toasts: Toast[]

  // actions
  setBackend: (status: AppState['backend']['status'], logPath?: string) => void
  loadWorkbook: (
    meta: SessionMeta,
    sheets: ParsedSheet[],
    filePath: string,
    origin: 'ocr' | 'excel'
  ) => void
  setView: (v: View) => void
  togglePanel: (p: 'pdf' | 'askAI') => void
  setPanel: (p: 'pdf' | 'askAI', open: boolean) => void
  setPanelWidth: (p: 'pdf' | 'askAI', px: number) => void
  onCitation: (cite: Citation) => void
  clearNavCell: () => void
  ask: (query: string) => Promise<void>
  setPdfPaths: (paths: string[]) => void
  setValidation: (v: ValidationSummary | null) => void
  setValidationEnabled: (v: boolean) => void
  setShowValidation: (v: boolean) => void
  setValidationPanel: (open: boolean) => void
  setWorkbookNotesDismissed: (v: boolean) => void
  setManualVerified: (issue: ValidationIssue, checked: boolean) => void
  selectCell: (sheet: string, cell: string) => void
  setSheetSources: (s: SheetSources) => void
  applySheetSources: (buf: ArrayBuffer, sheets: ParsedSheet[]) => Promise<void>
  setActiveSheet: (name: string) => void
  setActivePdf: (file: string | null) => void
  setActivePdfPage: (page: number | null) => void
  highlightPdf: (term: string | null) => void
  setSyncPdfToSheet: (v: boolean) => void
  focusSheetSource: (entry: SheetSourceEntry, page?: number) => void
  toggleShowSource: () => void
  setDirty: (dirty: boolean) => void
  markEdit: (sheet: string, a1: string) => void  // stamp a cell's last-edit time (history)
  save: (asNew?: boolean) => Promise<void>
  openWorkbookPath: (path: string, origin?: 'ocr' | 'excel') => Promise<boolean>
  reopenLast: () => Promise<void>
  openUpload: () => void
  closeUpload: () => void
  openAttachPdfs: () => void
  closeAttachPdfs: () => void
  openSettings: () => void
  closeSettings: () => void
  cancelDiscard: () => void
  discardAndUpload: () => void
  toast: (kind: Toast['kind'], text: string) => void
  dismissToast: (id: string) => void
}

let _tid = 0

// Whether the (beta) validation-review feature is enabled at all — a persisted user PREFERENCE
// set from Settings. Defaults to on. When off, the review bar and all highlighting are hidden.
const VALIDATION_ENABLED_KEY = 'fie.validationEnabled'
const loadValidationEnabled = (): boolean => {
  try {
    const v = localStorage.getItem(VALIDATION_ENABLED_KEY)
    return v === null ? true : v === '1'
  } catch {
    return true
  }
}

export const useApp = create<AppState>((set) => ({
  backend: { status: 'starting', logPath: '' },
  session: null,
  sheets: [],
  loadSeq: 0,
  cleanToken: 0,
  showSource: false,
  workbook: { dirty: false, filePath: null, origin: null },
  pdfPaths: [],
  validation: null,
  sheetSources: {},
  activeSheet: null,
  activePdf: null,
  activePdfPage: null,
  syncPdfToSheet: true,
  panels: { pdf: false, askAI: false },
  panelWidth: { pdf: 380, askAI: 400 },
  nav: { cell: null, pdfFile: null, pdfPage: null, pdfQuery: null, pdfQueryPage: null, pdfQueryPages: [], cellSeq: 0, pdfSeq: 0, pdfQuerySeq: 0 },
  chat: { messages: [], pending: false },
  view: 'home',
  uploadOpen: false,
  attachPdfsOpen: false,
  settingsOpen: false,
  confirmDiscard: false,
  validationLedger: null,
  validationEnabled: loadValidationEnabled(),
  showValidation: true,
  validationPanelOpen: false,
  workbookNotesDismissed: false,
  verifyWrites: {},
  sessionStart: null,
  editTimes: {},
  toasts: [],

  setBackend: (status, logPath) =>
    set((s) => ({ backend: { status, logPath: logPath ?? s.backend.logPath } })),
  loadWorkbook: (meta, sheets, filePath, origin) => {
    set((st) => ({
      session: meta,
      sheets,
      loadSeq: st.loadSeq + 1,
      workbook: { dirty: false, filePath, origin },
      chat: { messages: [], pending: false },
      // PDFs + lineage are workbook-specific — clear them so a prior workbook's source PDFs
      // don't leak into this one; review()/extraction (or the attach modal) sets them after.
      pdfPaths: [],
      validation: null,
      // build the read-only validation overlay once per load (null if no ledger sheet)
      validationLedger: buildValidationData(sheets),
      validationPanelOpen: false,
      workbookNotesDismissed: false,
      verifyWrites: {},
      // new session: stamp the open time (the "(session)" marker) and clear per-cell edit times
      sessionStart: nowLocalIso(),
      editTimes: {},
      sheetSources: {},
      activeSheet: null,
      activePdf: null,
      activePdfPage: null,
      nav: { cell: null, pdfFile: null, pdfPage: null, pdfQuery: null, pdfQueryPage: null, pdfQueryPages: [], cellSeq: 0, pdfSeq: 0, pdfQuerySeq: 0 },
      view: 'sheet',
      uploadOpen: false
    }))
    window.api.setDirty(false)
    window.api.setLastFile(filePath)
  },
  setView: (view) => set({ view }),
  togglePanel: (p) => set((s) => ({ panels: { ...s.panels, [p]: !s.panels[p] } })),
  setPanel: (p, open) => set((s) => ({ panels: { ...s.panels, [p]: open } })),
  setPanelWidth: (p, px) =>
    set((s) => ({ panelWidth: { ...s.panelWidth, [p]: Math.max(280, Math.min(720, px)) } })),
  onCitation: (cite) => {
    const loc = (cite.locator || {}) as Record<string, unknown>
    const url = (loc.link || loc.url) as string | undefined
    if (cite.kind === 'external' && url) {
      window.api.openExternal(url)
      return
    }
    const s = useApp.getState()
    // The workbook is ALWAYS present; the source PDF is optional. Open the relevant cell in
    // the grid AND — when that source PDF is actually loaded — jump the PDF to its page too.
    let target: { sheet: string; cell: string } | null = null
    if (loc.sheet && loc.cell) {
      target = { sheet: String(loc.sheet), cell: String(loc.cell) } // exact ledger cell
    } else if (loc.primary_sheet && loc.primary_cell) {
      target = { sheet: String(loc.primary_sheet), cell: String(loc.primary_cell) } // originating fact cell
    } else if (cite.kind === 'insight') {
      target = findInsightCell(s.sheets, loc) // Insights sheet row
    } else if (loc.sheet) {
      target = { sheet: String(loc.sheet), cell: String(loc.cell ?? 'A1') } // sheet-level
    } else if (loc.primary_sheet) {
      target = { sheet: String(loc.primary_sheet), cell: String(loc.primary_cell ?? 'A1') }
    }

    // PDF side: only if the cited report file is among the loaded PDFs (else skip silently —
    // the grid cell is the answer; don't toast "not loaded" when Excel already handled it).
    const base = (p: string) => p.replace(/\\/g, '/').split('/').pop()?.toLowerCase() ?? ''
    const file = ((loc.report_file ?? loc.file) as string | undefined) || null
    const page = loc.page != null ? Number(loc.page) : null
    const pdfLoaded = !!file && s.pdfPaths.some((p) => base(p) === file.toLowerCase())
    const navPdf = page != null && page >= 1 && pdfLoaded
    // value to highlight on the PDF page = the cited cell's value (e.g. the figure 182,625)
    const pdfQuery = navPdf && target ? cellValueAt(s.sheets, target.sheet, target.cell) : null
    // scope the highlight search to this sheet's source pages for the cited PDF
    const pdfQueryPages = navPdf && target ? sourcePagesFor(s.sheetSources, target.sheet, file) : []

    if (target || navPdf) {
      set((st) => ({
        view: target ? 'sheet' : st.view,
        panels: navPdf ? { ...st.panels, pdf: true } : st.panels,
        nav: {
          ...st.nav,
          // grid cell: bump cellSeq so the grid selects + scrolls to it
          cell: target ?? st.nav.cell,
          cellSeq: target ? st.nav.cellSeq + 1 : st.nav.cellSeq,
          // PDF page: bump pdfSeq so the viewer jumps (only when the PDF is loaded)
          pdfFile: navPdf ? file : st.nav.pdfFile,
          pdfPage: navPdf ? page : st.nav.pdfPage,
          pdfSeq: navPdf ? st.nav.pdfSeq + 1 : st.nav.pdfSeq,
          // PDF highlight: search the cited cell's value across the sheet's source pages
          pdfQuery: navPdf ? pdfQuery : st.nav.pdfQuery,
          pdfQueryPage: navPdf ? page : st.nav.pdfQueryPage,
          pdfQueryPages: navPdf ? pdfQueryPages : st.nav.pdfQueryPages,
          pdfQuerySeq: navPdf ? st.nav.pdfQuerySeq + 1 : st.nav.pdfQuerySeq
        }
      }))
      return
    }

    // Nothing in the workbook to point at and no loaded PDF — surface the page if the
    // citation has one (the viewer will note if that source isn't loaded), else just toast.
    if (page != null) {
      set((st) => ({
        panels: { ...st.panels, pdf: true },
        nav: { ...st.nav, cell: null, pdfFile: file, pdfPage: page, pdfQuery: null, pdfSeq: st.nav.pdfSeq + 1 }
      }))
      return
    }
    useApp.getState().toast('info', cite.display)
  },
  clearNavCell: () => set((s) => ({ nav: { ...s.nav, cell: null } })),
  ask: async (query) => {
    const s = useApp.getState()
    if (!s.session || s.chat.pending) return
    const uid = `u${++_tid}`
    const aid = `a${++_tid}`
    const now = Date.now()

    // Build conversation history from settled turns (exclude any still-pending assistant slot).
    // Assistant turns carry the resolved QueryFrame (compact, ~40 chars) instead of prose
    // (~1000+ chars), so we can send 8 full turns for the same token cost as 4 prose turns.
    const history = s.chat.messages
      .filter((m) => m.role === 'user' ? !!m.text : !!(m.response || m.error))
      .slice(-16)   // last 8 complete turns (user + assistant each)
      .map((m) => ({
        role: m.role,
        text: m.role === 'user' ? (m.text ?? '') : '',
        ...(m.role === 'assistant' && m.frame ? { frame: m.frame } : {})
      }))

    set((st) => ({
      chat: {
        pending: true,
        messages: [
          ...st.chat.messages,
          { id: uid, role: 'user', text: query, timestamp: now },
          { id: aid, role: 'assistant', timestamp: now }
        ]
      }
    }))
    // Send the current local time + unsaved edits so edit_history queries ("my unsaved
    // changes", "this session", "last 5 min") can be answered against the live grid state.
    const pending = pendingEditsForQuery(
      s.sheets.map((x) => x.name), s.sheets, s.editTimes, s.sessionStart ?? nowLocalIso()
    )
    const res = await api.answer(s.session.session_id, query, history, {
      client_now: nowLocalIso(),
      pending_edits: pending
    })
    set((st) => ({
      chat: {
        pending: false,
        messages: st.chat.messages.map((m) =>
          m.id !== aid
            ? m
            : res.status === 200
              ? { ...m, response: res.body, frame: res.body.frame }
              : {
                  ...m,
                  error:
                    (res.body as { detail?: string } | null)?.detail ??
                    `request failed (${res.status})`
                }
        )
      }
    }))
  },
  // Default the viewer to the LATEST-year PDF (not the first uploaded). Seeding activePdf
  // here also makes sheet-sync prefer the latest doc (pickSourceEntry favours activePdf).
  setPdfPaths: (paths) => set({ pdfPaths: paths, activePdf: latestPdfFile(paths) }),
  setValidation: (validation) => set({ validation }),
  setValidationEnabled: (validationEnabled) => {
    set({ validationEnabled })
    try {
      localStorage.setItem(VALIDATION_ENABLED_KEY, validationEnabled ? '1' : '0')
    } catch {
      /* localStorage unavailable — preference just won't persist */
    }
  },
  setShowValidation: (showValidation) => set({ showValidation }),
  setValidationPanel: (validationPanelOpen) => set({ validationPanelOpen }),
  setWorkbookNotesDismissed: (workbookNotesDismissed) => set({ workbookNotesDismissed }),
  // Toggle a row's "Manually Verified" flag. This is the ONLY workbook write in the feature:
  // it edits the ledger's Manually Verified cell via the live grid so the surgical (value-diff)
  // save persists it. We also flip the in-memory flag (so counts/tooltip/highlight update at
  // once) and live-recolour the flagged data cell green/back. Inert if the ledger has no
  // Manually Verified column (older workbook).
  setManualVerified: (issue, checked) => {
    const st = useApp.getState()
    const data = st.validationLedger
    if (!data) return
    issue.verified = checked // shared ref in cellIssue/sheetIssues/workbookNotes → all reflect it
    const mvA1 = `${data.mvCell}${issue.ledgerRow + 1}`
    writeCell(data.ledgerSheetName, mvA1, checked ? 'TRUE' : '')
    // live recolour the data cell (only while highlighting is shown)
    if (st.showValidation && issue.cell) {
      setCellBackground(issue.sheet, issue.cell, VALIDATION_BG[colorOf(issue)])
    }
    set({
      validationLedger: { ...data }, // new top ref → re-render
      verifyWrites: { ...st.verifyWrites, [`${data.ledgerSheetName}!${mvA1}`]: checked ? 'TRUE' : '' }
    })
  },
  // Navigate to a worksheet cell (used by the validation panel/chips). Mirrors the citation
  // path: switch to the sheet surface, mark the active sheet, and bump cellSeq so SheetView's
  // nav effect activates the sheet, selects the cell, and scrolls it into view.
  selectCell: (sheet, cell) =>
    set((st) => ({
      view: 'sheet',
      activeSheet: sheet,
      nav: { ...st.nav, cell: { sheet, cell }, cellSeq: st.nav.cellSeq + 1 }
    })),
  setSheetSources: (sheetSources) => set({ sheetSources: sheetSources ?? {} }),
  // Source the sheet→PDF map from the workbook's embedded `SheetSources` custom property;
  // fall back to reconstructing it from the Source Ledger sheet (older extracted files).
  applySheetSources: async (buf, sheets) => {
    let ss = await readSheetSources(buf)
    if (!Object.keys(ss).length) ss = reconstructSheetSources(sheets)
    set({ sheetSources: ss })
  },
  setActiveSheet: (name) => {
    const st = useApp.getState()
    if (st.activeSheet !== name) set({ activeSheet: name })
    // auto-sync the PDF to this sheet's source page (when enabled). Prefer the PDF the
    // user already has open — switching sheets keeps you in that document if it's one of
    // the sheet's sources; otherwise fall back to the primary (highest-weight) entry.
    // Does NOT force the PDF panel open — the badge click is the explicit "open & jump".
    if (!st.syncPdfToSheet) return
    // sheet-sync only applies to extracted workbooks with provenance — attached PDFs (e.g. on
    // an Excel workbook) have no lineage, so switching sheets must never move the viewer.
    if (!Object.keys(st.sheetSources).length) return
    const entry = pickSourceEntry(st.sheetSources[name], st.activePdf)
    if (!entry) return // no lineage for this sheet → leave PDF as-is
    const page = entry.pages?.[0]
    if (!entry.report_file || !page) return
    set((s) => ({
      nav: { ...s.nav, pdfFile: entry.report_file, pdfPage: page, pdfSeq: s.nav.pdfSeq + 1 }
    }))
  },
  setActivePdf: (file) => {
    const st = useApp.getState()
    if (st.activePdf === file) return
    set({ activePdf: file })
    // when the user switches PDFs, re-align the current sheet to a page within the
    // newly-opened PDF — but only if that PDF is actually a source for the sheet,
    // otherwise leave the viewer where the user put it.
    if (!st.syncPdfToSheet || !file || !st.activeSheet) return
    if (!Object.keys(st.sheetSources).length) return // attached PDFs have no lineage → no sync
    const entries = st.sheetSources[st.activeSheet]
    const match = entries?.find((e) => e.report_file.toLowerCase() === file.toLowerCase())
    const page = match?.pages?.[0]
    if (!match || !page) return
    set((s) => ({
      nav: { ...s.nav, pdfFile: match.report_file, pdfPage: page, pdfSeq: s.nav.pdfSeq + 1 }
    }))
  },
  setActivePdfPage: (page) => {
    if (useApp.getState().activePdfPage !== page) set({ activePdfPage: page })
  },
  // highlight a term across the active sheet's source pages for the open PDF — used when the
  // user selects a grid cell; the viewer searches those pages and jumps to the one with a hit.
  highlightPdf: (term) => {
    const st = useApp.getState()
    const pages =
      st.activeSheet && st.activePdf
        ? sourcePagesFor(st.sheetSources, st.activeSheet, st.activePdf)
        : []
    set((s) => ({
      nav: {
        ...s.nav,
        pdfQuery: term,
        pdfQueryPage: null,
        pdfQueryPages: pages,
        pdfQuerySeq: s.nav.pdfQuerySeq + 1
      }
    }))
  },
  setSyncPdfToSheet: (v) => {
    set({ syncPdfToSheet: v })
    // turning sync on re-aligns the PDF to whatever sheet is currently active
    const st = useApp.getState()
    if (v && st.activeSheet) st.setActiveSheet(st.activeSheet)
  },
  focusSheetSource: (entry, page) =>
    set((s) => ({
      panels: { ...s.panels, pdf: true }, // explicit click → ensure the PDF is visible
      // Claim the target PDF as active up front. Switching docs makes PdfPanel report
      // setActivePdf(file); if activePdf weren't already this file, that handler would
      // re-sync the sheet to the file's FIRST page and clobber the page picked here.
      activePdf: entry.report_file,
      nav: {
        ...s.nav,
        pdfFile: entry.report_file,
        pdfPage: page ?? entry.pages?.[0] ?? null,
        pdfSeq: s.nav.pdfSeq + 1
      }
    })),
  toggleShowSource: () => set((s) => ({ showSource: !s.showSource })),
  markEdit: (sheet, a1) => {
    // record WHEN a cell was edited so history windows ("last 5 min") are accurate. Best-effort
    // and additive — never throws; if the listener misses a cell the time falls back to save time.
    if (sheet === 'History' || sheet === '(session)') return // never track the log itself
    set((s) => ({ editTimes: { ...s.editTimes, [`${sheet}!${a1}`]: nowLocalIso() } }))
  },
  setDirty: (dirty) => {
    set((s) => ({ workbook: { ...s.workbook, dirty } }))
    window.api.setDirty(dirty)
  },
  save: async (asNew = false) => {
    const s = useApp.getState()
    if (!s.session || !s.workbook.filePath) return
    const visible = s.sheets.map((x) => x.name)
    try {
      const original = await window.api.readFile(s.workbook.filePath)
      const { bytes, warnings } = await buildEditedXlsx(original, visible, s.sheets, {
        editTimes: s.editTimes,
        sessionStart: s.sessionStart ?? nowLocalIso(),
        saveNow: nowLocalIso()
      })
      let path = s.workbook.filePath
      if (asNew || s.workbook.origin === 'ocr') {
        const suggested = `${s.session.company || 'workbook'}.xlsx`
        const chosen = await window.api.saveFile(suggested, bytes)
        if (!chosen) return // cancelled
        path = chosen
      } else {
        await window.api.writeFileAt(path, bytes)
      }
      // companion JSON (extracted metadata travels with the xlsx)
      const sidecar = path.replace(/\.xlsx$/i, '') + '.fie.json'
      const meta = JSON.stringify(
        { company: s.session.company, years: s.session.years, sheets: s.session.sheets },
        null,
        2
      )
      await window.api.writeFileAt(sidecar, new TextEncoder().encode(meta).buffer as ArrayBuffer)
      // re-ingest so Ask AI / Dashboard reflect the edits (cache-bust)
      const r = await window.api.reloadSession(s.session.session_id, path)
      set((st) => ({
        workbook: { ...st.workbook, dirty: false, filePath: path },
        cleanToken: st.cleanToken + 1, // re-baseline the grid's undo depth to "saved"
        verifyWrites: {} // Manually Verified writes are now persisted in the file
      }))
      window.api.setDirty(false)
      window.api.setLastFile(path)
      // Surface structural edits that the value-only save cannot persist (added/removed
      // sheets), so the user isn't misled into thinking those changes were written.
      for (const w of warnings) useApp.getState().toast('warning', w)
      useApp.getState().toast(r.status === 200 ? 'success' : 'warning',
        r.status === 200 ? 'Changes saved successfully' : 'Saved to disk, but re-ingest failed')
    } catch (e) {
      useApp.getState().toast('error', `Save failed: ${String(e)}`)
    }
  },
  openWorkbookPath: async (path, origin = 'excel') => {
    const res = await window.api.createSession(path)
    if (res.status !== 200) {
      useApp.getState().toast('error', 'Could not open workbook')
      return false
    }
    const meta = res.body as SessionMeta
    const buf = await window.api.readFile(path)
    const sheets = await parseWorkbook(buf, meta.sheets)
    useApp.getState().loadWorkbook(meta, sheets, path, origin)
    // sheet→PDF lineage from the workbook's embedded SheetSources property (falls back to
    // the Source Ledger sheet) — works once the matching source PDFs are attached.
    await useApp.getState().applySheetSources(buf, sheets)
    return true
  },
  reopenLast: async () => {
    const p = await window.api.getLastFile()
    if (p) useApp.getState().openWorkbookPath(p, 'excel')
  },
  openUpload: () => {
    // unsaved edits -> ask first via the "Discard Changes?" modal; otherwise go straight in
    if (useApp.getState().workbook.dirty) {
      set({ confirmDiscard: true })
      return
    }
    set({ uploadOpen: true })
  },
  closeUpload: () => set({ uploadOpen: false }),
  openAttachPdfs: () => set({ attachPdfsOpen: true }),
  closeAttachPdfs: () => set({ attachPdfsOpen: false }),
  openSettings: () => set({ settingsOpen: true }),
  closeSettings: () => set({ settingsOpen: false }),
  cancelDiscard: () => set({ confirmDiscard: false }),
  discardAndUpload: () => {
    // abandon unsaved changes and open the upload screen
    set({ confirmDiscard: false, uploadOpen: true })
    useApp.getState().setDirty(false)
  },
  toast: (kind, text) => set((s) => ({ toasts: [...s.toasts, { id: `t${++_tid}`, kind, text }] })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }))
}))
