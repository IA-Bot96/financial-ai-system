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
  error?: string
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
  panels: { pdf: boolean; askAI: boolean }
  panelWidth: { pdf: number; askAI: number }
  nav: { cell: { sheet: string; cell: string } | null; pdfPage: number | null; seq: number }
  chat: { messages: ChatTurn[]; pending: boolean }
  view: View
  uploadOpen: boolean
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
  toggleShowSource: () => void
  setDirty: (dirty: boolean) => void
  save: (asNew?: boolean) => Promise<void>
  openWorkbookPath: (path: string, origin?: 'ocr' | 'excel') => Promise<boolean>
  reopenLast: () => Promise<void>
  openUpload: () => void
  closeUpload: () => void
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
  panels: { pdf: false, askAI: false },
  panelWidth: { pdf: 380, askAI: 400 },
  nav: { cell: null, pdfPage: null, seq: 0 },
  chat: { messages: [], pending: false },
  view: 'home',
  uploadOpen: false,
  toasts: [],

  setBackend: (status, logPath) =>
    set((s) => ({ backend: { status, logPath: logPath ?? s.backend.logPath } })),
  loadWorkbook: (meta, sheets, filePath, origin) => {
    set((st) => ({
      session: meta,
      sheets,
      loadSeq: st.loadSeq + 1,
      workbook: { dirty: false, filePath, origin },
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
        nav: { cell: { sheet: String(loc.sheet), cell: String(loc.cell) }, pdfPage: null, seq: s.nav.seq + 1 }
      }))
      return
    }
    if (loc.page != null) {
      set((s) => ({
        panels: { ...s.panels, pdf: true },
        nav: { cell: null, pdfPage: Number(loc.page), seq: s.nav.seq + 1 }
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
    set((st) => ({
      chat: {
        pending: true,
        messages: [
          ...st.chat.messages,
          { id: uid, role: 'user', text: query },
          { id: aid, role: 'assistant' }
        ]
      }
    }))
    const res = await api.answer(s.session.session_id, query)
    set((st) => ({
      chat: {
        pending: false,
        messages: st.chat.messages.map((m) =>
          m.id !== aid
            ? m
            : res.status === 200
              ? { ...m, response: res.body }
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
      const bytes = buildEditedXlsx(original, visible)
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
    const s = useApp.getState()
    if (s.workbook.dirty && !window.confirm('You have unsaved changes. Discard them?')) return
    set({ uploadOpen: true })
  },
  closeUpload: () => set({ uploadOpen: false }),
  toast: (kind, text) => set((s) => ({ toasts: [...s.toasts, { id: `t${++_tid}`, kind, text }] })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }))
}))
