import type { ParsedSheet } from './sheetjs'
import { getSnapshot } from './sheetApi'
import { patchXlsx, colToA1, type CellEdit } from './xlsxPatch'
import { diffSheet, type SnapCellData } from './saveDiff'
import { historyEdits, type HistEntry, HISTORY_SHEET, SESSION_SHEET } from './history'

export interface SaveResult {
  bytes: ArrayBuffer
  /** Non-fatal warnings to surface to the user (e.g. structural edits that don't persist). */
  warnings: string[]
  /** True when this save appended rows to the Edit History sheet (incl. the session marker),
   *  so the caller can stop re-writing the once-per-session "opened" marker. */
  historyWritten: boolean
}

/** One unsaved change to send to the backend with a query (so "my unsaved changes" works). */
export interface PendingEdit {
  timestamp: string
  sheet: string
  cell: string
  old: string
  new: string
}

/** Session context the save uses to write the History block. */
export interface HistoryCtx {
  editTimes: Record<string, string> // "sheet!A1" -> ISO time the cell was last edited
  sessionStart: string // ISO time the workbook was opened this session (-> "(session)" marker)
  saveNow: string // fallback timestamp for edits with no recorded time
  includeMarker: boolean // write the "(session) opened" row (true only on the FIRST save)
}

const baseVal = (b: ParsedSheet | undefined, r: number, c: number): string => {
  const v = b?.cellData?.[r]?.[c]?.v
  return v == null ? '' : String(v)
}

/** First empty row (0-based) of a sheet, from its populated cells — NOT ParsedSheet.rows,
 *  which is padded. Header-only History (row 0) -> 1; prior-session rows -> after them. */
function nextRow(base: ParsedSheet | undefined): number {
  let max = -1
  for (const r in base?.cellData ?? {}) {
    const n = Number(r)
    if (n > max) max = n
  }
  return max + 1
}

/**
 * Live diff of the visible sheets vs the loaded baseline -> change entries {sheet, A1, old,
 * new, timestamp}. Undo/redo net out for free (a cell returned to baseline isn't in the diff).
 * The History sheet itself is never included (invariant 1). Used to send unsaved edits with a
 * query; prepend the session marker for "this session".
 */
export function collectChangeEntries(
  visibleNames: string[],
  baseline: ParsedSheet[],
  editTimes: Record<string, string>,
  fallbackTs: string
): PendingEdit[] {
  const sheets = getSnapshot()?.sheets
  if (!sheets) return []
  const baseByName = new Map(baseline.map((s) => [s.name, s]))
  const out: PendingEdit[] = []
  for (const id in sheets) {
    const name = sheets[id]?.name
    if (!name || name === HISTORY_SHEET || !visibleNames.includes(name)) continue
    const base = baseByName.get(name)
    for (const e of diffSheet(base, (sheets[id].cellData as SnapCellData) || {})) {
      const a1 = colToA1(e.col) + (e.row + 1)
      out.push({
        sheet: name,
        cell: a1,
        old: baseVal(base, e.row, e.col),
        new: e.value == null ? '' : String(e.value),
        timestamp: editTimes[`${name}!${a1}`] ?? fallbackTs
      })
    }
  }
  return out
}

/** Unsaved edits + the session-open marker, to send with a query. */
export function pendingEditsForQuery(
  visibleNames: string[],
  baseline: ParsedSheet[],
  editTimes: Record<string, string>,
  sessionStart: string
): PendingEdit[] {
  return [
    { timestamp: sessionStart, sheet: SESSION_SHEET, cell: '', old: '', new: 'opened' },
    ...collectChangeEntries(visibleNames, baseline, editTimes, sessionStart)
  ]
}

/**
 * Detect structural edits the value-only patch CANNOT persist: added / removed / renamed
 * sheets. (Row/column insert-delete inside a sheet is not detected here — it surfaces as a
 * burst of cell-value edits — and is a documented limitation of the surgical save.)
 */
function structuralWarnings(baseline: ParsedSheet[], snapshotNames: string[]): string[] {
  const baseNames = new Set(baseline.map((s) => s.name))
  const snapNames = new Set(snapshotNames)
  const added = [...snapNames].filter((n) => !baseNames.has(n))
  const removed = [...baseNames].filter((n) => !snapNames.has(n))
  const warnings: string[] = []
  if (added.length) warnings.push(`Added sheet(s) won't be saved: ${added.join(', ')}`)
  if (removed.length) warnings.push(`Removed sheet(s) won't be saved: ${removed.join(', ')}`)
  return warnings
}

/**
 * Build edited xlsx bytes losslessly. Starts from the ORIGINAL workbook bytes and splices
 * in ONLY the user's changed cell values (diffed against the loaded `baseline`), preserving
 * every formula, style, number format, merge, frozen pane, column width, cell comment, and
 * `calcPr/fullCalcOnLoad` — and every untouched sheet — exactly as the pipeline wrote them.
 *
 * Cached formula results are preserved as written; downstream subtotals are NOT recomputed
 * into the file (the pipeline sets `fullCalcOnLoad`, so Excel/LibreOffice recalc on open).
 *
 * If no snapshot/edits are available, or patching throws, returns the original bytes
 * unchanged: the worst case is "this save captured no edit", never a corrupted workbook.
 */
export async function buildEditedXlsx(
  originalBytes: ArrayBuffer,
  visibleNames: string[],
  baseline: ParsedSheet[],
  history?: HistoryCtx
): Promise<SaveResult> {
  try {
    const snap = getSnapshot()
    const sheets = snap?.sheets
    if (!sheets) return { bytes: originalBytes, warnings: [], historyWritten: false }

    const baseByName = new Map(baseline.map((s) => [s.name, s]))
    const editsBySheet = new Map<string, CellEdit[]>()
    const snapshotNames: string[] = []
    for (const id in sheets) {
      const s = sheets[id]
      const name = s?.name
      if (!name) continue
      snapshotNames.push(name)
      if (!visibleNames.includes(name)) continue
      const edits = diffSheet(baseByName.get(name), (s.cellData as SnapCellData) || {})
      if (edits.length) editsBySheet.set(name, edits)
    }

    const warnings = structuralWarnings(baseline, snapshotNames)
    if (!editsBySheet.size) return { bytes: originalBytes, warnings, historyWritten: false }

    // Append this save's changes to the Edit History sheet WITHIN this same patch (so they are
    // persisted, not left as a fresh unsaved change — no save loop). Only when the sheet exists
    // (extraction-seeded; the surgical patch cannot create a sheet). The caller re-parses the
    // saved bytes as the new baseline, so each save records only the NEW changes since the last
    // (incremental, append-only); the "(session) opened" marker is written once per session.
    let historyWritten = false
    if (history) {
      const baseHist = baseByName.get(HISTORY_SHEET)
      if (baseHist) {
        const rows: HistEntry[] = history.includeMarker
          ? [{ ts: history.sessionStart, sheet: SESSION_SHEET, cell: '', old: '', new: 'opened', saved: true }]
          : []
        for (const [name, edits] of editsBySheet) {
          if (name === HISTORY_SHEET) continue
          const base = baseByName.get(name)
          for (const e of edits) {
            const a1 = colToA1(e.col) + (e.row + 1)
            rows.push({
              ts: history.editTimes[`${name}!${a1}`] ?? history.saveNow,
              sheet: name,
              cell: a1,
              old: baseVal(base, e.row, e.col),
              new: e.value == null ? '' : String(e.value),
              saved: true
            })
          }
        }
        const hEdits = historyEdits(rows, nextRow(baseHist))
        if (hEdits.length) {
          editsBySheet.set(HISTORY_SHEET, hEdits)
          historyWritten = true
        }
      }
    }

    const bytes = await patchXlsx(originalBytes, editsBySheet)
    return { bytes, warnings, historyWritten }
  } catch (e) {
    console.error('[save] surgical patch failed; keeping original bytes (no edits applied)', e)
    return { bytes: originalBytes, warnings: [], historyWritten: false }
  }
}
