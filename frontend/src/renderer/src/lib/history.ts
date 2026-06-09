/**
 * Change-history helpers (framework-agnostic).
 *
 * The app records the user's cell edits and, on save, appends them as rows to the workbook's
 * `History` sheet (datetime | sheet | cell | old | new | saved). The FIE backend reads that
 * sheet to answer "what did I change" questions; unsaved edits are sent alongside each query.
 *
 * Design (see lib/save.ts for the wiring): unsaved edits and the save-time history rows are
 * BOTH derived from the live value-diff (current grid vs the loaded baseline), so undo/redo
 * net out for free — a cell returned to baseline simply isn't in the diff. This module only
 * provides the shared constants + the row→CellEdit serialization; the dirty/unsaved signal is
 * Univer's own undo stack (SheetView), and the save loop is avoided because:
 *   1. writes to the `History` sheet are excluded from the diff (no history-of-history);
 *   2. the History sheet is read-only in the grid (SheetView vetoes edits to it);
 *   3. history rows are written WITHIN the save patch (to the file, not the grid), so they
 *      never add to the undo stack or re-dirty the workbook.
 */
import type { CellEdit } from './xlsxPatch'

// NB: NOT "History" — that name is RESERVED by Excel/ExcelJS, and the app's ExcelJS parser
// throws when loading a workbook that contains a sheet named "History", silently falling back
// to a values-only parse (dropping styles + formulas for the WHOLE workbook). Must match the
// extraction seeding + backend classify_sheet exactly.
export const HISTORY_SHEET = 'Edit History'
export const SESSION_SHEET = '(session)' // sentinel "sheet" for the workbook-opened marker

export interface HistEntry {
  ts: string // ISO local datetime
  sheet: string
  cell: string // A1, '' for the session marker
  old: string
  new: string
  saved: boolean
}

/** Local-clock ISO timestamp (no 'Z') — `YYYY-MM-DDTHH:MM:SS`. Used for edit times, the
 *  session marker, and `client_now`, so they share the user's clock (what "last 5 min" and
 *  "this session" mean to them) and the backend parses them uniformly via fromisoformat. */
export function nowLocalIso(d: Date = new Date()): string {
  const p = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(
    d.getMinutes()
  )}:${p(d.getSeconds())}`
}

/** Serialize history entries → CellEdits on the History sheet, starting at 0-based `startRow`
 *  (= the first empty row under the existing rows). patchXlsx inserts the new <row>/<c> for
 *  these (it already supports previously-absent rows). */
export function historyEdits(entries: HistEntry[], startRow: number): CellEdit[] {
  const edits: CellEdit[] = []
  entries.forEach((e, i) => {
    const cols = [e.ts, e.sheet, e.cell, e.old, e.new, e.saved ? 'TRUE' : 'FALSE']
    cols.forEach((v, c) => edits.push({ row: startRow + i, col: c, value: v }))
  })
  return edits
}
