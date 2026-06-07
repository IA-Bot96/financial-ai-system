import type { ParsedSheet } from './sheetjs'
import { getSnapshot } from './sheetApi'
import { patchXlsx, type CellEdit } from './xlsxPatch'
import { diffSheet, type SnapCellData } from './saveDiff'

export interface SaveResult {
  bytes: ArrayBuffer
  /** Non-fatal warnings to surface to the user (e.g. structural edits that don't persist). */
  warnings: string[]
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
  baseline: ParsedSheet[]
): Promise<SaveResult> {
  try {
    const snap = getSnapshot()
    const sheets = snap?.sheets
    if (!sheets) return { bytes: originalBytes, warnings: [] }

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
    if (!editsBySheet.size) return { bytes: originalBytes, warnings }
    const bytes = await patchXlsx(originalBytes, editsBySheet)
    return { bytes, warnings }
  } catch (e) {
    console.error('[save] surgical patch failed; keeping original bytes (no edits applied)', e)
    return { bytes: originalBytes, warnings: [] }
  }
}
