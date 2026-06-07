import { create } from 'zustand'
import type { ParsedSheet } from '@/lib/sheetjs'
import { api, type Citation, type FieResponse } from '@/api'
import { parseWorkbook } from '@/lib/sheetjs'
import { buildEditedXlsx } from '@/lib/save'

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
  syncPdfToSheet: boolean          // auto-jump the PDF to the active sheet's source page
  panels: { pdf: boolean; askAI: boolean }
  panelWidth: { pdf: number; askAI: number }
  nav: {
    cell: { sheet: string; cell: string } | null
    pdfFile: string | null         // target PDF (by filename); null = keep current
    pdfPage: number | null
    seq: number
  }
  chat: { messages: ChatTurn[]; pending: boolean }
  view: View
  uploadOpen: boolean
  confirmDiscard: boolean // "Discard Changes?" prompt before navigating to upload (New)
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
  setSheetSources: (s: SheetSources) => void
  setActiveSheet: (name: string) => void
  setSyncPdfToSheet: (v: boolean) => void
  focusSheetSource: (entry: SheetSourceEntry, page?: number) => void
  toggleShowSource: () => void
  setDirty: (dirty: boolean) => void
  save: (asNew?: boolean) => Promise<void>
  openWorkbookPath: (path: string, origin?: 'ocr' | 'excel') => Promise<boolean>
  reopenLast: () => Promise<void>
  openUpload: () => void
  closeUpload: () => void
  cancelDiscard: () => void
  discardAndUpload: () => void
  toast: (kind: Toast['kind'], text: string) => void
  dismissToast: (id: string) => void
}

let _tid = 0

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
  syncPdfToSheet: true,
  panels: { pdf: false, askAI: false },
  panelWidth: { pdf: 380, askAI: 400 },
  nav: { cell: null, pdfFile: null, pdfPage: null, seq: 0 },
  chat: { messages: [], pending: false },
  view: 'home',
  uploadOpen: false,
  confirmDiscard: false,
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
      // lineage is workbook-specific — clear it; review()/extraction sets it after load
      sheetSources: {},
      activeSheet: null,
      nav: { cell: null, pdfFile: null, pdfPage: null, seq: 0 },
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
    if (loc.sheet && loc.cell) {
      set((s) => ({
        view: 'sheet',
        nav: {
          cell: { sheet: String(loc.sheet), cell: String(loc.cell) },
          pdfFile: null,
          pdfPage: null,
          seq: s.nav.seq + 1
        }
      }))
      return
    }
    if (loc.page != null) {
      set((s) => ({
        panels: { ...s.panels, pdf: true },
        nav: {
          cell: null,
          // citations carry a page only — let the viewer keep the current PDF
          pdfFile: (loc.report_file as string) ?? (loc.file as string) ?? null,
          pdfPage: Number(loc.page),
          seq: s.nav.seq + 1
        }
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
    const res = await api.answer(s.session.session_id, query, history)
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
  setPdfPaths: (paths) => set({ pdfPaths: paths }),
  setValidation: (validation) => set({ validation }),
  setSheetSources: (sheetSources) => set({ sheetSources: sheetSources ?? {} }),
  setActiveSheet: (name) => {
    const st = useApp.getState()
    if (st.activeSheet !== name) set({ activeSheet: name })
    // auto-sync the PDF to this sheet's primary source page (when enabled).
    // Does NOT force the PDF panel open — non-intrusive on plain tab switches; the
    // badge click (focusSheetSource) is the explicit "open & jump" affordance.
    if (!st.syncPdfToSheet) return
    const entries = st.sheetSources[name]
    if (!entries || !entries.length) return // no lineage for this sheet → leave PDF as-is
    const primary = entries[0]
    const page = primary.pages?.[0]
    if (!primary.report_file || !page) return
    set((s) => ({
      nav: { ...s.nav, pdfFile: primary.report_file, pdfPage: page, seq: s.nav.seq + 1 }
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
      nav: {
        ...s.nav,
        pdfFile: entry.report_file,
        pdfPage: page ?? entry.pages?.[0] ?? null,
        seq: s.nav.seq + 1
      }
    })),
  toggleShowSource: () => set((s) => ({ showSource: !s.showSource })),
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
      const { bytes, warnings } = await buildEditedXlsx(original, visible, s.sheets)
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
        cleanToken: st.cleanToken + 1 // re-baseline the grid's undo depth to "saved"
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
    const sheets = await parseWorkbook(await window.api.readFile(path), meta.sheets)
    useApp.getState().loadWorkbook(meta, sheets, path, origin)
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
  cancelDiscard: () => set({ confirmDiscard: false }),
  discardAndUpload: () => {
    // abandon unsaved changes and open the upload screen
    set({ confirmDiscard: false, uploadOpen: true })
    useApp.getState().setDirty(false)
  },
  toast: (kind, text) => set((s) => ({ toasts: [...s.toasts, { id: `t${++_tid}`, kind, text }] })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }))
}))
