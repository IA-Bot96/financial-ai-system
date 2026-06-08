import ExcelJS from 'exceljs'
import JSZip from 'jszip'
import * as XLSX from 'xlsx'
import type { SheetSources, SheetSourceEntry } from '@/store'

export interface SheetMeta {
  name: string
  role: string
  editable: boolean
}

type CellVal = string | number | boolean
interface ParsedCell {
  v?: CellVal // cached value (may be absent for formula cells written without a result)
  f?: string // formula text incl. leading "=" (Univer recalculates these, incl. cross-sheet)
  st?: Record<string, unknown>
}
interface Merge {
  startRow: number
  startColumn: number
  endRow: number
  endColumn: number
}

export interface ParsedSheet extends SheetMeta {
  cellData: Record<number, Record<number, ParsedCell>>
  merges: Merge[]
  colWidth: Record<number, number>
  rows: number
  cols: number
}

const argb6 = (argb?: string): string | null => (argb ? '#' + argb.slice(-6) : null)

/** Normalize an ExcelJS cell value (formula/richtext/hyperlink/date) to a primitive. */
function valueOf(v: unknown): CellVal | null {
  if (v == null) return null
  if (typeof v === 'object') {
    const o = v as Record<string, unknown>
    if ('result' in o) return (o.result as CellVal) ?? null // formula -> cached result
    if ('text' in o) return o.text as CellVal // hyperlink
    if ('richText' in o)
      return (o.richText as { text: string }[]).map((r) => r.text).join('')
    if (v instanceof Date) return v.toISOString().slice(0, 10)
    return null
  }
  return v as CellVal
}

/** ExcelJS cell -> Univer IStyleData-ish object (font/fill/align/number-format). */
function styleOf(cell: ExcelJS.Cell): Record<string, unknown> | null {
  const out: Record<string, unknown> = {}
  const f = cell.font
  if (f?.bold) out.bl = 1
  if (f?.italic) out.it = 1
  if (f?.size) out.fs = f.size
  if (f?.name) out.ff = f.name
  const fc = argb6((f?.color as { argb?: string } | undefined)?.argb)
  if (fc) out.cl = { rgb: fc }
  const fill = cell.fill as { type?: string; fgColor?: { argb?: string } } | undefined
  if (fill?.type === 'pattern') {
    const bg = argb6(fill.fgColor?.argb)
    if (bg) out.bg = { rgb: bg }
  }
  const ha = cell.alignment?.horizontal
  if (ha) out.ht = ha === 'center' ? 2 : ha === 'right' ? 3 : 1
  if (cell.numFmt) out.n = { pattern: cell.numFmt }
  return Object.keys(out).length ? out : null
}

const A1 = /([A-Z]+)(\d+)/
function colToIdx(letters: string): number {
  let n = 0
  for (const ch of letters) n = n * 26 + (ch.charCodeAt(0) - 64)
  return n - 1
}
function decodeMerge(ref: string): Merge | null {
  const [a, b] = ref.split(':')
  const ma = A1.exec(a)
  const mb = A1.exec(b || a)
  if (!ma || !mb) return null
  return {
    startRow: Number(ma[2]) - 1,
    startColumn: colToIdx(ma[1]),
    endRow: Number(mb[2]) - 1,
    endColumn: colToIdx(mb[1])
  }
}

/**
 * ExcelJS 4.4 crashes in `reconcile` ("Cannot read properties of undefined (reading
 * 'comments')") on some workbooks that carry cell comments — it fails to resolve the
 * comment part referenced by a worksheet relationship, which aborts the whole parse and
 * loses every style. We don't render comments anyway, so strip the comment parts (and
 * their relationships / legacy VML anchors) from the xlsx zip and let ExcelJS parse the
 * rest with full styling intact.
 */
async function stripComments(buf: ArrayBuffer): Promise<ArrayBuffer> {
  const zip = await JSZip.loadAsync(buf)
  const remove: string[] = []
  zip.forEach((p) => {
    // openpyxl (the pipeline writer) emits xl/comments/comment{n}.xml + a
    // xl/drawings/commentsDrawing{n}.vml anchor; other writers use xl/comments{n}.xml /
    // xl/threadedComments/…. Match all so the comment-crash retry actually strips them.
    if (/xl\/(comments\/comment\d+|(threaded)?comments\d*|threadedComments\/threadedComment\d+)\.xml$/i.test(p))
      remove.push(p)
    if (/xl\/drawings\/(vmlDrawing\d*|commentsDrawing\d+)\.vml$/i.test(p)) remove.push(p)
  })
  remove.forEach((p) => zip.remove(p))

  // drop the comment/vml relationship entries so reconcile never looks them up
  const rels: string[] = []
  zip.forEach((p) => {
    if (/_rels\/[^/]*\.rels$/i.test(p)) rels.push(p)
  })
  for (const rp of rels) {
    const xml = await zip.file(rp)!.async('string')
    const cleaned = xml.replace(/<Relationship\b[^>]*\/>/gi, (tag) =>
      /comments|vmlDrawing/i.test(tag) ? '' : tag
    )
    if (cleaned !== xml) zip.file(rp, cleaned)
  }
  // remove the <legacyDrawing> anchors that referenced the now-deleted vml rels
  const wsheets: string[] = []
  zip.forEach((p) => {
    if (/xl\/worksheets\/sheet\d+\.xml$/i.test(p)) wsheets.push(p)
  })
  for (const sp of wsheets) {
    const xml = await zip.file(sp)!.async('string')
    const cleaned = xml.replace(/<legacyDrawing\b[^>]*\/>/gi, '')
    if (cleaned !== xml) zip.file(sp, cleaned)
  }
  return zip.generateAsync({ type: 'arraybuffer' })
}

const decodeXml = (s: string) =>
  s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&amp;/g, '&') // last, so other entities aren't double-decoded

/**
 * Read the sheet→PDF-page map the extraction pipeline embeds as the `SheetSources` custom
 * document property (docProps/custom.xml). This travels inside the workbook, so it works on
 * any opened file — including re-opened downloads — without the job API or a sidecar.
 * Returns {} when the property is absent or unparseable (feature simply inactive).
 */
export async function readSheetSources(buf: ArrayBuffer): Promise<SheetSources> {
  try {
    const zip = await JSZip.loadAsync(buf)
    const xml = await zip.file('docProps/custom.xml')?.async('string')
    if (!xml) return {}
    const prop = xml.match(/<property\b[^>]*\bname="SheetSources"[^>]*>([\s\S]*?)<\/property>/i)
    if (!prop) return {}
    const val =
      prop[1].match(/<vt:lpwstr>([\s\S]*?)<\/vt:lpwstr>/i) ??
      prop[1].match(/<vt:bstr>([\s\S]*?)<\/vt:bstr>/i)
    if (!val) return {}
    const parsed = JSON.parse(decodeXml(val[1].trim())) as Record<string, unknown>
    if (!parsed || typeof parsed !== 'object') return {}

    const out: SheetSources = {}
    for (const [sheet, raw] of Object.entries(parsed)) {
      if (!Array.isArray(raw)) continue
      const entries: SheetSourceEntry[] = raw
        .map((e) => {
          const o = (e ?? {}) as Record<string, unknown>
          const pages = Array.isArray(o.pages)
            ? o.pages.map(Number).filter((n) => Number.isFinite(n) && n >= 1)
            : []
          return {
            report_file: String(o.report_file ?? ''),
            pages,
            table_ids: Array.isArray(o.table_ids) ? o.table_ids.map(String) : [],
            weight: Number(o.weight ?? 0)
          }
        })
        .filter((e) => e.report_file && e.pages.length)
        .sort((a, b) => b.weight - a.weight) // highest-weight first (defensive)
      if (entries.length) out[sheet] = entries
    }
    return out
  } catch {
    return {} // older file / malformed property → feature inactive, never error
  }
}

/** Primary parse via ExcelJS (full styles). May throw in some renderer environments. */
async function parseExcel(buf: ArrayBuffer, meta: SheetMeta[]): Promise<ParsedSheet[]> {
  const wb = new ExcelJS.Workbook()
  await wb.xlsx.load(buf)
  const byName = new Map(meta.map((m) => [m.name, m]))
  const out: ParsedSheet[] = []

  wb.eachSheet((ws) => {
    const cellData: ParsedSheet['cellData'] = {}
    let maxR = 0
    let maxC = 0
    ws.eachRow({ includeEmpty: false }, (row, r) => {
      row.eachCell({ includeEmpty: false }, (cell, c) => {
        const raw = cell.value as unknown
        const isFormula =
          raw != null &&
          typeof raw === 'object' &&
          ('formula' in (raw as object) || 'sharedFormula' in (raw as object))
        // cell.formula returns the translated formula (no leading "="); add it back.
        const f = isFormula && cell.formula ? '=' + cell.formula : undefined
        const v = valueOf(cell.value) // cached result; often absent for openpyxl formulas
        // keep the cell if it has a value OR a formula (don't drop formulas with no cache)
        if (!f && (v === undefined || v === null || v === '')) return
        const st = styleOf(cell)
        const out: ParsedCell = {}
        if (v !== undefined && v !== null && v !== '') out.v = v
        if (f) out.f = f
        if (st) out.st = st
        ;(cellData[r - 1] ||= {})[c - 1] = out
        if (r - 1 > maxR) maxR = r - 1
        if (c - 1 > maxC) maxC = c - 1
      })
    })
    const merges: Merge[] = (((ws.model as { merges?: string[] }).merges) || [])
      .map(decodeMerge)
      .filter((m): m is Merge => m !== null)
    const colWidth: Record<number, number> = {}
    ws.columns?.forEach((col, i) => {
      if (col?.width) colWidth[i] = Math.round(col.width * 7)
    })
    const m = byName.get(ws.name)
    out.push({
      name: ws.name,
      role: m?.role ?? 'unknown',
      editable: m?.editable ?? true,
      cellData,
      merges,
      colWidth,
      rows: Math.max(maxR + 50, 100),
      cols: Math.max(maxC + 8, 26)
    })
  })
  return out
}

/** Fallback parse via SheetJS — values only, no styles. Keeps the app working if
 * ExcelJS can't run in the current environment. */
function parseValues(buf: ArrayBuffer, meta: SheetMeta[]): ParsedSheet[] {
  const wb = XLSX.read(buf, { type: 'array' })
  const byName = new Map(meta.map((m) => [m.name, m]))
  const out: ParsedSheet[] = []
  for (const name of wb.SheetNames) {
    const ws = wb.Sheets[name]
    const ref = ws && ws['!ref'] ? ws['!ref'] : 'A1'
    const range = XLSX.utils.decode_range(ref)
    const cellData: ParsedSheet['cellData'] = {}
    for (let r = range.s.r; r <= range.e.r; r++) {
      for (let c = range.s.c; c <= range.e.c; c++) {
        const cell = ws ? ws[XLSX.utils.encode_cell({ r, c })] : undefined
        const v = cell?.v
        if (v !== undefined && v !== null && v !== '')
          (cellData[r] ||= {})[c] = { v: v as CellVal }
      }
    }
    const m = byName.get(name)
    out.push({
      name,
      role: m?.role ?? 'unknown',
      editable: m?.editable ?? true,
      cellData,
      merges: [],
      colWidth: {},
      rows: Math.max(range.e.r + 50, 100),
      cols: Math.max(range.e.c + 8, 26)
    })
  }
  return out
}

/** Log how much styling ExcelJS actually applied (0 styled cells => file is unstyled). */
function logStyleTally(sheets: ParsedSheet[], note: string): void {
  let styled = 0
  let merges = 0
  let numFmt = 0
  for (const s of sheets) {
    merges += s.merges.length
    for (const r in s.cellData)
      for (const c in s.cellData[r]) {
        const st = s.cellData[r][c].st
        if (st) {
          styled++
          if ('n' in st) numFmt++
        }
      }
  }
  console.log(
    `[parse] ${note}: ${sheets.length} sheets, ${styled} styled cells ` +
      `(${numFmt} with number formats), ${merges} merges`
  )
}

/**
 * Parse xlsx bytes with full styles. ExcelJS is primary; if it throws (a known
 * comment-reconcile crash on some workbooks), strip the comment parts and retry — this
 * recovers all styling. SheetJS values-only is the last-resort fallback.
 */
export async function parseWorkbook(buf: ArrayBuffer, meta: SheetMeta[]): Promise<ParsedSheet[]> {
  try {
    const sheets = await parseExcel(buf, meta)
    logStyleTally(sheets, 'ExcelJS')
    return sheets
  } catch (e1) {
    console.warn('[parse] ExcelJS threw; retrying with comment parts stripped', e1)
    try {
      const sheets = await parseExcel(await stripComments(buf), meta)
      logStyleTally(sheets, 'ExcelJS (comments stripped)')
      return sheets
    } catch (e2) {
      console.error('[parse] ExcelJS failed even after cleanup; values-only (no styles)', e2)
      return parseValues(buf, meta)
    }
  }
}

/**
 * Build a Univer IWorkbookData object (deduped styles registry + merges + col widths).
 *
 * `validationCellSeverity` (optional) is a render-only overlay: sheet name → A1 coord →
 * severity. Matching cells get a background tint merged into their style. This is purely
 * decorative — the surgical save is value-only (see saveDiff.ts), so these styles are never
 * written back. Toggling the overlay rebuilds this data; nothing mutates the workbook.
 */
export function toUniverData(
  sheets: ParsedSheet[],
  validationColors?: Record<string, Record<string, 'error' | 'warning' | 'minor' | 'verified'>>
): Record<string, unknown> {
  const sheetOrder: string[] = []
  const sheetMap: Record<string, unknown> = {}
  const styles: Record<string, unknown> = {}
  const styleIds = new Map<string, string>()
  // soft tints (kept in sync with validation.ts VALIDATION_BG)
  const VBG = { error: '#FEE2E2', warning: '#FEF3C7', minor: '#F1F5F9', verified: '#DCFCE7' } as const

  const styleId = (st: Record<string, unknown>): string => {
    const key = JSON.stringify(st)
    let id = styleIds.get(key)
    if (!id) {
      id = `s${styleIds.size + 1}`
      styleIds.set(key, id)
      styles[id] = st
    }
    return id
  }

  sheets.forEach((s, i) => {
    const id = `sheet-${i}`
    sheetOrder.push(id)
    // pre-index this sheet's validation coords as "row,col" → colour key
    const sevByRC = new Map<string, 'error' | 'warning' | 'minor' | 'verified'>()
    const sevMap = validationColors?.[s.name]
    if (sevMap) {
      for (const a1 in sevMap) {
        const m = A1.exec(a1.toUpperCase())
        if (m) sevByRC.set(`${Number(m[2]) - 1},${colToIdx(m[1])}`, sevMap[a1])
      }
    }
    const cellData: Record<number, Record<number, { v?: CellVal; f?: string; s?: string }>> = {}
    for (const r in s.cellData) {
      for (const c in s.cellData[r]) {
        const cell = s.cellData[r][c]
        const conv: { v?: CellVal; f?: string; s?: string } = {}
        if (cell.v !== undefined) conv.v = cell.v
        if (cell.f) conv.f = cell.f // Univer's formula engine recalculates this on load
        const sev = sevByRC.get(`${r},${c}`)
        const st = sev ? { ...(cell.st ?? {}), bg: { rgb: VBG[sev] } } : cell.st
        if (st) conv.s = styleId(st)
        ;(cellData[Number(r)] ||= {})[Number(c)] = conv
      }
    }
    // validation coords that point at an empty (uncreated) cell still get a tinted cell
    for (const [rc, sev] of sevByRC) {
      const [rr, cc] = rc.split(',').map(Number)
      if (cellData[rr]?.[cc]) continue
      ;(cellData[rr] ||= {})[cc] = { s: styleId({ bg: { rgb: VBG[sev] } }) }
    }
    const columnData: Record<number, { w: number }> = {}
    for (const c in s.colWidth) columnData[Number(c)] = { w: s.colWidth[c] }
    sheetMap[id] = {
      id,
      name: s.name,
      cellData,
      rowCount: s.rows,
      columnCount: s.cols,
      mergeData: s.merges,
      columnData
    }
  })
  return { id: 'fie-wb', name: 'workbook', sheetOrder, sheets: sheetMap, styles }
}
