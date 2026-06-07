import type { ParsedSheet } from './sheetjs'
import { getSnapshot } from './sheetApi'
import { patchXlsx, type CellEdit, type CellValue } from './xlsxPatch'

type SnapCell = { v?: CellValue; f?: string }
type SnapCellData = Record<string, Record<string, SnapCell>>

/** Numeric-aware equality so float/Univer reformatting doesn't read as an edit. */
function sameValue(a: CellValue | undefined, b: CellValue | undefined): boolean {
  const aEmpty = a === undefined || a === null || a === ''
  const bEmpty = b === undefined || b === null || b === ''
  if (aEmpty && bEmpty) return true
  if (aEmpty !== bEmpty) return false
  const na = Number(a)
  const nb = Number(b)
  if (Number.isFinite(na) && Number.isFinite(nb)) return na === nb
  return String(a) === String(b)
}

/**
 * Diff the live Univer snapshot of one sheet against the baseline that was loaded into
 * the grid, returning only the cells the user actually changed.
 *
 * Hard rule: a FORMULA cell is never emitted as an edit — neither one that is a formula
 * in the snapshot, nor one that was a formula in the baseline (the snapshot may carry
 * only its cached value). This guarantees untouched formulas stay formulas on save.
 */
function diffSheet(base: ParsedSheet | undefined, snap: SnapCellData): CellEdit[] {
  const edits: CellEdit[] = []
  const baseCells = base?.cellData ?? {}

  for (const rs in snap) {
    const r = Number(rs)
    const rowSnap = snap[rs]
    for (const cs in rowSnap) {
      const c = Number(cs)
      const sCell = rowSnap[cs] || {}
      if (sCell.f) continue // snapshot formula — leave the original formula intact
      const bCell = baseCells[r]?.[c]
      if (bCell?.f) continue // baseline was a formula — never flatten it to a value
      if (!sameValue(sCell.v, bCell?.v)) {
        edits.push({ row: r, col: c, value: sCell.v ?? null })
      }
    }
  }

  // User-cleared cells: present (non-formula) in baseline, now empty in the snapshot.
  // Only emit when the snapshot has the cell present-but-empty — if the cell is absent
  // from the snapshot we assume Univer pruned it, NOT that the user cleared it (avoids
  // accidentally wiping data).
  for (const rs in baseCells) {
    const r = Number(rs)
    for (const cs in baseCells[rs]) {
      const c = Number(cs)
      const bCell = baseCells[rs][cs]
      if (bCell?.f || bCell?.v === undefined || bCell?.v === null || bCell?.v === '') continue
      const sCell = snap[rs]?.[cs]
      if (sCell && !sCell.f && (sCell.v === undefined || sCell.v === null || sCell.v === '')) {
        edits.push({ row: r, col: c, value: null })
      }
    }
  }

  return edits
}

/**
 * Build edited xlsx bytes losslessly. Starts from the ORIGINAL workbook bytes and splices
 * in ONLY the user's changed cell values (diffed against the loaded `baseline`), preserving
 * every formula, style, number format, merge, frozen pane, column width, cell comment, and
 * `calcPr/fullCalcOnLoad` — and every untouched sheet — exactly as the pipeline wrote them.
 *
 * If no snapshot/edits are available, or patching throws, returns the original bytes
 * unchanged: the worst case is "this save captured no edit", never a corrupted workbook.
 */
export async function buildEditedXlsx(
  originalBytes: ArrayBuffer,
  visibleNames: string[],
  baseline: ParsedSheet[]
): Promise<ArrayBuffer> {
  try {
    const snap = getSnapshot()
    const sheets = snap?.sheets
    if (!sheets) return originalBytes

    const baseByName = new Map(baseline.map((s) => [s.name, s]))
    const editsBySheet = new Map<string, CellEdit[]>()
    for (const id in sheets) {
      const s = sheets[id]
      const name = s?.name
      if (!name || !visibleNames.includes(name)) continue
      const edits = diffSheet(baseByName.get(name), (s.cellData as SnapCellData) || {})
      if (edits.length) editsBySheet.set(name, edits)
    }
    if (!editsBySheet.size) return originalBytes
    return await patchXlsx(originalBytes, editsBySheet)
  } catch (e) {
    console.error('[save] surgical patch failed; keeping original bytes (no edits applied)', e)
    return originalBytes
  }
}
