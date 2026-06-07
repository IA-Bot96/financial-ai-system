/**
 * Surgical .xlsx patcher — round-trip-lossless save.
 *
 * The OCR pipeline writes workbooks far richer than a value grid: formulas, per-cell
 * styles, number formats, merged cells, frozen panes, column widths, cell comments
 * (audit provenance), and `calcPr/fullCalcOnLoad`. Rebuilding a sheet from the render
 * model (the old aoa_to_sheet + SheetJS-community path) silently dropped ALL of these.
 *
 * The only way to honour the round-trip contract — "load → edit → save is lossless for
 * everything the pipeline wrote, even cells/sheets the user never touched" — is to keep
 * the ORIGINAL file bytes as the source of truth and splice in ONLY the user's changed
 * cell values, leaving every other byte (styles.xml, comments parts, sharedStrings,
 * workbook calcPr, merges/panes/cols, and all untouched sheets) exactly as written.
 *
 * We therefore:
 *   1. open the original xlsx zip (JSZip),
 *   2. map each edited sheet NAME to its `xl/worksheets/sheetN.xml` part,
 *   3. string-splice each changed `<c>` element in-place (replacing only that element's
 *      bytes; everything around it is untouched), inserting `<c>`/`<row>` in sorted order
 *      when the user typed into a previously-absent cell,
 *   4. re-zip.
 *
 * Formula cells are NEVER patched (see diff in save.ts) so untouched formulas stay
 * formulas. String edits are written as inline strings (`t="inlineStr"`) so the shared
 * string table is left byte-for-byte intact.
 */
import JSZip from 'jszip'

export type CellValue = string | number | boolean | null
export interface CellEdit {
  row: number // 0-based
  col: number // 0-based
  value: CellValue // null or '' => clear the cell (keep its style)
}

/** 0-based column index -> A1 letters (0 -> "A", 26 -> "AA"). */
export function colToA1(col: number): string {
  let n = col + 1
  let s = ''
  while (n > 0) {
    const r = (n - 1) % 26
    s = String.fromCharCode(65 + r) + s
    n = Math.floor((n - 1) / 26)
  }
  return s
}

/** A1 letters -> 0-based column index. */
function a1ToCol(letters: string): number {
  let n = 0
  for (const ch of letters) n = n * 26 + (ch.charCodeAt(0) - 64)
  return n - 1
}

function escapeXml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

const RE_SPECIAL = /[.*+?^${}()|[\]\\]/g
const esc = (s: string): string => s.replace(RE_SPECIAL, '\\$&')

/** Build the replacement `<c>` element, preserving the existing style attribute. */
function buildCell(ref: string, styleAttr: string, value: CellValue): string {
  // styleAttr is e.g. ' s="3"' (with leading space) or '' — preserved verbatim.
  if (value === null || value === '') return `<c r="${ref}"${styleAttr}/>`
  if (typeof value === 'boolean') return `<c r="${ref}"${styleAttr} t="b"><v>${value ? 1 : 0}</v></c>`
  if (typeof value === 'number' && Number.isFinite(value))
    return `<c r="${ref}"${styleAttr}><v>${value}</v></c>`
  // string (or non-finite number coerced to text): inline string keeps sharedStrings.xml untouched
  return `<c r="${ref}"${styleAttr} t="inlineStr"><is><t xml:space="preserve">${escapeXml(
    String(value)
  )}</t></is></c>`
}

/** Extract the ` s="N"` style attribute (with its leading space) from a `<c>`'s attributes. */
function styleAttrOf(attrs: string): string {
  const m = /\ss="\d+"/.exec(attrs)
  return m ? m[0] : ''
}

/**
 * Replace or insert one cell in a worksheet XML string. Returns the new XML.
 * Byte-preserving for all content other than the single `<c>` (and, when inserting,
 * the surrounding `<row>`/`<sheetData>` open tags).
 */
function upsertCell(xml: string, edit: CellEdit): string {
  const ref = colToA1(edit.col) + (edit.row + 1)
  const rowNum = edit.row + 1

  // 1) Existing cell: replace just that <c>…</c> (or self-closing <c/>), keeping its style.
  const cellRe = new RegExp(`<c r="${esc(ref)}"([^>]*?)(/>|>[\\s\\S]*?</c>)`)
  const cellMatch = cellRe.exec(xml)
  if (cellMatch) {
    const replacement = buildCell(ref, styleAttrOf(cellMatch[1]), edit.value)
    return xml.slice(0, cellMatch.index) + replacement + xml.slice(cellMatch.index + cellMatch[0].length)
  }

  // Nothing to insert when clearing a cell that doesn't exist.
  if (edit.value === null || edit.value === '') return xml

  const newCell = buildCell(ref, '', edit.value)

  // 2) Row exists: insert the new <c> in column order within that row.
  const rowOpenRe = new RegExp(`<row r="${rowNum}"([^>]*?)(/>|>)`)
  const rowMatch = rowOpenRe.exec(xml)
  if (rowMatch) {
    if (rowMatch[2] === '/>') {
      // self-closing empty row -> expand to hold the cell
      const open = `<row r="${rowNum}"${rowMatch[1]}>`
      const expanded = `${open}${newCell}</row>`
      return xml.slice(0, rowMatch.index) + expanded + xml.slice(rowMatch.index + rowMatch[0].length)
    }
    // find this row's closing tag, then choose an insertion point among its cells
    const rowStart = rowMatch.index
    const closeIdx = xml.indexOf('</row>', rowStart)
    if (closeIdx !== -1) {
      const rowBody = xml.slice(rowMatch.index + rowMatch[0].length, closeIdx)
      const cellOpenRe = /<c r="([A-Z]+)\d+"/g
      let insertAt = closeIdx // default: just before </row>
      let m: RegExpExecArray | null
      while ((m = cellOpenRe.exec(rowBody)) !== null) {
        if (a1ToCol(m[1]) > edit.col) {
          insertAt = rowMatch.index + rowMatch[0].length + m.index
          break
        }
      }
      return xml.slice(0, insertAt) + newCell + xml.slice(insertAt)
    }
  }

  // 3) Row absent: insert a new <row> in row order within <sheetData>.
  const newRow = `<row r="${rowNum}">${newCell}</row>`
  const sdSelf = /<sheetData\s*\/>/.exec(xml)
  if (sdSelf) {
    const replacement = `<sheetData>${newRow}</sheetData>`
    return xml.slice(0, sdSelf.index) + replacement + xml.slice(sdSelf.index + sdSelf[0].length)
  }
  const sdOpen = /<sheetData[^>]*>/.exec(xml)
  if (sdOpen) {
    const sdBodyStart = sdOpen.index + sdOpen[0].length
    const sdClose = xml.indexOf('</sheetData>', sdBodyStart)
    const body = xml.slice(sdBodyStart, sdClose === -1 ? undefined : sdClose)
    const rowRe = /<row r="(\d+)"/g
    let insertAt = sdClose === -1 ? sdBodyStart : sdClose
    let m: RegExpExecArray | null
    while ((m = rowRe.exec(body)) !== null) {
      if (Number(m[1]) > rowNum) {
        insertAt = sdBodyStart + m.index
        break
      }
    }
    return xml.slice(0, insertAt) + newRow + xml.slice(insertAt)
  }
  // No <sheetData> at all (shouldn't happen for a real sheet) — leave unchanged.
  return xml
}

/** Apply all edits to one worksheet XML string. */
export function applyEditsToSheetXml(xml: string, edits: CellEdit[]): string {
  // Sort by row then col so multi-cell insertions into new rows land in valid order.
  const sorted = [...edits].sort((a, b) => a.row - b.row || a.col - b.col)
  let out = xml
  for (const e of sorted) out = upsertCell(out, e)
  return out
}

/** name -> "xl/worksheets/sheetN.xml" via workbook.xml + its rels. */
async function sheetPathMap(zip: JSZip): Promise<Map<string, string>> {
  const map = new Map<string, string>()
  const wbFile = zip.file('xl/workbook.xml')
  const relsFile = zip.file('xl/_rels/workbook.xml.rels')
  if (!wbFile || !relsFile) return map
  const wbXml = await wbFile.async('string')
  const relsXml = await relsFile.async('string')

  // rId -> target path (normalized under xl/)
  const ridToTarget = new Map<string, string>()
  const relRe = /<Relationship\b[^>]*>/g
  let rm: RegExpExecArray | null
  while ((rm = relRe.exec(relsXml)) !== null) {
    const tag = rm[0]
    const id = /\bId="([^"]+)"/.exec(tag)?.[1]
    let target = /\bTarget="([^"]+)"/.exec(tag)?.[1]
    if (!id || !target) continue
    target = target.replace(/^\/?xl\//, '').replace(/^\//, '')
    ridToTarget.set(id, `xl/${target}`)
  }

  // sheet name -> rId (in document order; we only need the mapping, not the order here)
  const sheetRe = /<sheet\b[^>]*>/g
  let sm: RegExpExecArray | null
  while ((sm = sheetRe.exec(wbXml)) !== null) {
    const tag = sm[0]
    const name = /\bname="([^"]*)"/.exec(tag)?.[1]
    const rid = /\br:id="([^"]+)"/.exec(tag)?.[1] ?? /\br:id='([^']+)'/.exec(tag)?.[1]
    if (!name || !rid) continue
    const path = ridToTarget.get(rid)
    if (path) map.set(decodeXmlEntities(name), path)
  }
  return map
}

function decodeXmlEntities(s: string): string {
  return s
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&')
}

/**
 * Patch the original xlsx bytes with the given per-sheet cell edits. Everything not
 * explicitly edited is preserved byte-for-byte. Returns the new file bytes.
 */
export async function patchXlsx(
  originalBytes: ArrayBuffer,
  editsBySheet: Map<string, CellEdit[]>
): Promise<ArrayBuffer> {
  const zip = await JSZip.loadAsync(originalBytes)
  const nameToPath = await sheetPathMap(zip)
  for (const [name, edits] of editsBySheet) {
    if (!edits.length) continue
    const path = nameToPath.get(name)
    if (!path) continue
    const file = zip.file(path)
    if (!file) continue
    const xml = await file.async('string')
    const patched = applyEditsToSheetXml(xml, edits)
    if (patched !== xml) zip.file(path, patched)
  }
  return zip.generateAsync({ type: 'arraybuffer' })
}
