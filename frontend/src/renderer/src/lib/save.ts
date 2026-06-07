import * as XLSX from 'xlsx'
import { getSnapshot } from './sheetApi'

type Cell = { v?: string | number | boolean }
type CellData = Record<string, Record<string, Cell>>

/** cellData {row:{col:{v}}} -> a SheetJS worksheet. */
function cellDataToSheet(cellData: CellData): XLSX.WorkSheet {
  let maxR = 0
  let maxC = 0
  for (const r in cellData) {
    maxR = Math.max(maxR, Number(r))
    for (const c in cellData[r]) maxC = Math.max(maxC, Number(c))
  }
  const aoa: (string | number | boolean | null)[][] = []
  for (let r = 0; r <= maxR; r++) {
    const row: (string | number | boolean | null)[] = []
    for (let c = 0; c <= maxC; c++) {
      const v = cellData[r]?.[c]?.v
      row.push(v === undefined ? null : v)
    }
    aoa.push(row)
  }
  return XLSX.utils.aoa_to_sheet(aoa)
}

/**
 * Build edited xlsx bytes: start from the ORIGINAL workbook (preserves meta-sheets we
 * never loaded into the grid), then overwrite each visible sheet from the live Univer
 * snapshot. If no snapshot is available, returns the original bytes unchanged (safe — no
 * data loss; edits just aren't captured). GUI round-trip verification pending.
 */
export function buildEditedXlsx(originalBytes: ArrayBuffer, visibleNames: string[]): ArrayBuffer {
  const wb = XLSX.read(originalBytes, { type: 'array' })
  const snap = getSnapshot()
  const sheets = snap?.sheets || {}
  for (const id in sheets) {
    const s = sheets[id]
    const name = s?.name
    if (!name || !visibleNames.includes(name)) continue
    try {
      const ws = cellDataToSheet((s.cellData as CellData) || {})
      wb.Sheets[name] = ws
      if (!wb.SheetNames.includes(name)) wb.SheetNames.push(name)
    } catch {
      /* keep original sheet on failure */
    }
  }
  const out = XLSX.write(wb, { type: 'array', bookType: 'xlsx' })
  return out as ArrayBuffer
}
