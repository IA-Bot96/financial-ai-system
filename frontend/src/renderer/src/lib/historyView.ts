/**
 * Read model for the History panel. Builds the full change list from the workbook's
 * "Edit History" sheet (saved rows) merged with the live unsaved edits, classifies each row
 * (cell edit / manual verify-unverify / workbook load), resolves a verify row to the financial
 * cell it refers to (via the Validation Ledger), and provides client-side search + filtering.
 * Pure functions — no React/Univer deps beyond the snapshot read inside pendingEditsForQuery.
 */
import type { ParsedSheet } from './sheetjs'
import { pendingEditsForQuery, type PendingEdit } from './save'
import { HISTORY_SHEET, SESSION_SHEET } from './history'

const VALIDATION_LEDGER = 'Validation Ledger'

export type HistType = 'edit' | 'verify' | 'unverify' | 'load'

export interface HistEntry {
  ts: number | null // epoch ms (for sort + date filter); null if unparseable
  tsRaw: string
  sheet: string // raw sheet of the write ("(session)" / "Validation Ledger" / a financial sheet)
  cell: string
  old: string
  new: string
  saved: boolean
  type: HistType
  verifiedSheet?: string // for verify/unverify: the financial sheet the ledger row refers to
  verifiedCell?: string
}

const truthy = (v: string): boolean => /^(true|1|yes)$/i.test(v.trim())
const cellStr = (row: Record<number, { v?: unknown }> | undefined, c: number): string => {
  const v = row?.[c]?.v
  return v == null ? '' : String(v)
}

/** Rows persisted in the "Edit History" sheet. */
function parseSaved(sheets: ParsedSheet[]): Omit<HistEntry, 'ts' | 'type'>[] {
  const eh = sheets.find((s) => s.name === HISTORY_SHEET)
  if (!eh) return []
  const cd = eh.cellData || {}
  const out: Omit<HistEntry, 'ts' | 'type'>[] = []
  for (const r of Object.keys(cd).map(Number).filter((r) => r >= 1).sort((a, b) => a - b)) {
    const row = cd[r]
    const tsRaw = cellStr(row, 0)
    const sheet = cellStr(row, 1)
    if (!tsRaw && !sheet) continue
    out.push({
      tsRaw,
      sheet,
      cell: cellStr(row, 2),
      old: cellStr(row, 3),
      new: cellStr(row, 4),
      saved: truthy(cellStr(row, 5))
    })
  }
  return out
}

/** Map a Validation-Ledger row number ("J5" -> 5) to the financial (sheet, cell) it validates. */
function ledgerResolver(sheets: ParsedSheet[]): (rowNum: number) => { sheet?: string; cell?: string } {
  const vl = sheets.find((s) => s.name === VALIDATION_LEDGER)
  if (!vl) return () => ({})
  const cd = vl.cellData || {}
  const hdr = cd[0] || {}
  let sheetCol = -1
  let cellCol = -1
  for (const c of Object.keys(hdr).map(Number)) {
    const h = String(hdr[c]?.v ?? '').toLowerCase()
    if (sheetCol < 0 && h.includes('sheet')) sheetCol = c
    if (cellCol < 0 && h.includes('cell')) cellCol = c
  }
  return (rowNum) => {
    const row = cd[rowNum - 1] // A1 row N -> 0-based cellData index N-1 (header at index 0)
    if (!row) return {}
    return {
      sheet: sheetCol >= 0 ? cellStr(row, sheetCol) || undefined : undefined,
      cell: cellCol >= 0 ? cellStr(row, cellCol) || undefined : undefined
    }
  }
}

function classify(sheet: string, newv: string): HistType {
  if (sheet === SESSION_SHEET || sheet === '(session)') return 'load'
  if (sheet === VALIDATION_LEDGER) return truthy(newv) ? 'verify' : 'unverify'
  return 'edit'
}

function parseTs(s: string): number | null {
  if (!s) return null
  const t = Date.parse(s)
  return Number.isNaN(t) ? null : t
}

const keyOf = (tsRaw: string, sheet: string, cell: string): string => `${tsRaw}|${sheet}|${cell}`

/** Full change list (saved + live unsaved), newest first. */
export function buildHistory(
  sheets: ParsedSheet[],
  editTimes: Record<string, string>,
  sessionStart: string,
  excludeCells?: Set<string>
): HistEntry[] {
  const seen = new Set<string>()
  const raws: Omit<HistEntry, 'ts' | 'type'>[] = []
  for (const r of parseSaved(sheets)) {
    seen.add(keyOf(r.tsRaw, r.sheet, r.cell))
    raws.push(r)
  }
  const pending: PendingEdit[] = pendingEditsForQuery(
    sheets.map((s) => s.name),
    sheets,
    editTimes,
    sessionStart,
    excludeCells
  )
  for (const p of pending) {
    const k = keyOf(p.timestamp, p.sheet, p.cell)
    if (seen.has(k)) continue
    seen.add(k)
    raws.push({ tsRaw: p.timestamp, sheet: p.sheet, cell: p.cell, old: p.old || '', new: p.new || '', saved: false })
  }

  const resolve = ledgerResolver(sheets)
  const entries: HistEntry[] = []
  for (const r of raws) {
    // skip the app-added "Manually Verified" COLUMN header (schema, not a user action)
    if (r.sheet === VALIDATION_LEDGER && r.new.trim().toLowerCase() === 'manually verified') continue
    const type = classify(r.sheet, r.new)
    const e: HistEntry = { ...r, type, ts: parseTs(r.tsRaw) }
    if (type === 'verify' || type === 'unverify') {
      const m = /(\d+)\s*$/.exec(r.cell)
      if (m) {
        const v = resolve(Number(m[1]))
        e.verifiedSheet = v.sheet
        e.verifiedCell = v.cell
      }
    }
    entries.push(e)
  }
  entries.sort((a, b) => (b.ts ?? 0) - (a.ts ?? 0)) // newest first
  return entries
}

/** Financial sheet an entry belongs to for the Sheet filter (verify -> the validated sheet;
 *  edit -> its sheet; load -> none). */
export function effectiveSheet(e: HistEntry): string | undefined {
  if (e.type === 'load') return undefined
  if (e.type === 'verify' || e.type === 'unverify') return e.verifiedSheet || VALIDATION_LEDGER
  return e.sheet
}

export const TYPE_LABELS: Record<HistType, string> = {
  edit: 'Cell Edit',
  load: 'Workbook uploaded',
  verify: 'Manually verified',
  unverify: 'Manually unverified'
}

export interface HistFilters {
  search: string
  types: string[] // selected TYPE_LABELS
  sheets: string[] // selected effective-sheet names
  date: string | null // 'YYYY-MM-DD' or null (= all dates)
}

export function applyFilters(entries: HistEntry[], f: HistFilters): HistEntry[] {
  const q = f.search.trim().toLowerCase()
  const typeSet = new Set(f.types)
  const sheetSet = new Set(f.sheets)
  let from = -Infinity
  let to = Infinity
  if (f.date) {
    const start = new Date(`${f.date}T00:00:00`).getTime()
    const end = new Date(`${f.date}T23:59:59.999`).getTime()
    if (!Number.isNaN(start)) {
      from = start
      to = end
    }
  }
  return entries.filter((e) => {
    if (!typeSet.has(TYPE_LABELS[e.type])) return false
    const es = effectiveSheet(e)
    if (es !== undefined && !sheetSet.has(es)) return false // load entries bypass the sheet filter
    if (f.date && (e.ts == null || e.ts < from || e.ts > to)) return false
    if (q) {
      const hay = [e.sheet, e.cell, e.old, e.new, e.verifiedSheet, e.verifiedCell]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      if (!hay.includes(q)) return false
    }
    return true
  })
}
