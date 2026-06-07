/**
 * Pure diff logic for the surgical save path (no browser/Univer deps, so it is unit
 * testable in plain Node). Given the baseline that was loaded into the grid and the live
 * Univer snapshot of one sheet, it returns ONLY the cell-value changes the user made.
 */
import type { ParsedSheet } from './sheetjs'
import type { CellEdit, CellValue } from './xlsxPatch'

export type SnapCell = { v?: CellValue; f?: string }
export type SnapCellData = Record<string, Record<string, SnapCell>>

const isEmpty = (v: CellValue | undefined): boolean => v === undefined || v === null || v === ''

/** Numeric-aware equality so float/Univer reformatting doesn't read as an edit. */
export function sameValue(a: CellValue | undefined, b: CellValue | undefined): boolean {
  const aEmpty = isEmpty(a)
  const bEmpty = isEmpty(b)
  if (aEmpty && bEmpty) return true
  if (aEmpty !== bEmpty) return false
  const na = Number(a)
  const nb = Number(b)
  if (Number.isFinite(na) && Number.isFinite(nb)) return na === nb
  return String(a) === String(b)
}

/**
 * Diff the live snapshot of one sheet against the baseline loaded into the grid.
 *
 * Rules:
 *  - FORMULA cells are never emitted — neither a cell that is a formula in the snapshot,
 *    nor one that was a formula in the baseline (the snapshot may carry only its cached
 *    value). Untouched formulas therefore stay formulas on save, and a cell the user
 *    turned INTO a formula is left to the original (formula authoring isn't persisted —
 *    a documented limitation).
 *  - Value changes (including typing into a previously-absent/empty cell) are emitted.
 *  - CLEARS are emitted when a cell that held a non-formula value in the baseline is now
 *    empty OR absent from the snapshot. (Univer only drops a cell from its data model when
 *    it no longer holds a value, so "baseline had a value, snapshot has none" is a genuine
 *    user clear — emitting `{value:null}` removes the original <v> while keeping the style.)
 */
export function diffSheet(base: ParsedSheet | undefined, snap: SnapCellData): CellEdit[] {
  const edits: CellEdit[] = []
  const baseCells = base?.cellData ?? {}

  // 1) Value changes / new cells — non-empty snapshot values that differ from baseline.
  for (const rs in snap) {
    const r = Number(rs)
    const rowSnap = snap[rs]
    for (const cs in rowSnap) {
      const c = Number(cs)
      const sCell = rowSnap[cs] || {}
      if (sCell.f) continue // snapshot formula — leave the original intact
      if (isEmpty(sCell.v)) continue // empty snapshot value — handled by the clear pass
      const bCell = baseCells[r]?.[c]
      if (bCell?.f) continue // baseline was a formula — never flatten it to a value
      if (!sameValue(sCell.v, bCell?.v)) edits.push({ row: r, col: c, value: sCell.v ?? null })
    }
  }

  // 2) Clears — baseline held a non-formula value, snapshot now has none (empty or absent).
  for (const rs in baseCells) {
    const r = Number(rs)
    for (const cs in baseCells[rs]) {
      const c = Number(cs)
      const bCell = baseCells[rs][cs]
      if (bCell?.f || isEmpty(bCell?.v)) continue
      const sCell = snap[rs]?.[cs]
      if (sCell?.f) continue // user turned it into a formula — not a clear; leave original
      if (isEmpty(sCell?.v)) edits.push({ row: r, col: c, value: null })
    }
  }

  return edits
}
