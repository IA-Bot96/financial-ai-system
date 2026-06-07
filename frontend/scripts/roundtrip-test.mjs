/**
 * Round-trip preservation test for the surgical xlsx patcher.
 *
 * Builds a fixture workbook resembling a pipeline output (formula, styles, number format,
 * merged cells, frozen pane, cell comment, fullCalcOnLoad), applies ONE value edit through
 * the real patch code (transpiled from src/lib/xlsxPatch.ts), then asserts that everything
 * except the edited cell is byte-for-byte intact in the .xlsx parts.
 *
 * Run:  node scripts/roundtrip-test.mjs
 */
import { build } from 'esbuild'
import ExcelJS from 'exceljs'
import JSZip from 'jszip'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { rm, readFile, readdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

let failures = 0
const assert = (cond, msg) => {
  if (cond) console.log(`  PASS  ${msg}`)
  else {
    console.error(`  FAIL  ${msg}`)
    failures++
  }
}

/** Transpile a src TS module -> a temp ESM module and import it. */
async function loadTsModule(relPath, tmpName) {
  const out = path.join(ROOT, 'scripts', tmpName)
  await build({
    entryPoints: [path.join(ROOT, relPath)],
    bundle: true,
    format: 'esm',
    platform: 'node',
    outfile: out,
    external: ['jszip'],
    logLevel: 'silent'
  })
  const mod = await import(pathToFileURL(out).href)
  return { mod, cleanup: () => rm(out, { force: true }) }
}

/** Build a fixture xlsx with the rich features the contract requires preserving. */
async function buildFixture() {
  const wb = new ExcelJS.Workbook()
  const ws = wb.addWorksheet('P&L')

  ws.getCell('A1').value = 'Revenue'
  ws.getCell('A2').value = 'Cost'
  ws.getCell('A3').value = 'Gross Profit'

  // values
  ws.getCell('B1').value = 1000
  ws.getCell('B2').value = 400
  // formula (must remain a formula on save)
  ws.getCell('B3').value = { formula: 'B1-B2', result: 600 }

  // per-cell style + number format
  ws.getCell('B1').numFmt = '#,##0;(#,##0);\\-'
  ws.getCell('B2').numFmt = '#,##0;(#,##0);\\-'
  ws.getCell('A1').font = { bold: true, color: { argb: 'FFFFFFFF' } }
  ws.getCell('A1').fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF1F4E79' } }

  // merged cells
  ws.mergeCells('A5:C5')
  ws.getCell('A5').value = 'Note banner'

  // frozen pane
  ws.views = [{ state: 'frozen', xSplit: 1, ySplit: 3 }]

  // cell comment (provenance) — the contract's primary concern
  ws.getCell('B3').note = 'OVERRIDE: substituted audited Gross Profit = 600 (was 590). Source: AR2024 p12'

  // fullCalcOnLoad
  wb.calcProperties = wb.calcProperties || {}
  wb.calcProperties.fullCalcOnLoad = true

  const buf = await wb.xlsx.writeBuffer()
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
}

const partOf = async (zip, name) => {
  const f = zip.file(name)
  return f ? await f.async('string') : null
}

/** Item 1 & 3 unit tests over the pure diff (clears, formula protection, new cells). */
async function testDiff() {
  console.log('\nDiff logic (clears / formula protection / new cells):')
  const { mod, cleanup } = await loadTsModule('src/renderer/src/lib/saveDiff.ts', '.saveDiff.gen.mjs')
  try {
    const { diffSheet } = mod
    const base = {
      name: 'S',
      role: 'r',
      editable: true,
      merges: [],
      colWidth: {},
      rows: 10,
      cols: 10,
      cellData: {
        0: { 0: { v: 'Revenue' }, 1: { v: 1000 } }, // A1 string, B1 number
        2: { 1: { v: 600, f: '=B1-B2' } } // B3 formula (cached 600)
      }
    }

    // (a) clear via present-empty snapshot cell
    let edits = diffSheet(base, { 0: { 1: { v: '' } } })
    assert(edits.some((e) => e.row === 0 && e.col === 1 && e.value === null),
      'clear emitted when snapshot cell is present-but-empty')

    // (b) clear via ABSENT snapshot cell (Univer pruned it) — item #1's core concern
    edits = diffSheet(base, { 0: { 0: { v: 'Revenue' } } }) // B1 absent entirely
    assert(edits.some((e) => e.row === 0 && e.col === 1 && e.value === null),
      'clear emitted when snapshot pruned the cleared cell (absent)')

    // (c) formula cell never emitted even if snapshot shows a different cached value
    edits = diffSheet(base, { 2: { 1: { v: 9999 } } }) // no f in snapshot, different v
    assert(!edits.some((e) => e.row === 2 && e.col === 1),
      'baseline formula cell never emitted (stays a formula)')

    // (d) new cell typed into a previously-absent location
    edits = diffSheet(base, { 5: { 5: { v: 42 } } })
    assert(edits.some((e) => e.row === 5 && e.col === 5 && e.value === 42),
      'new value in a previously-absent cell emitted')

    // (e) unchanged numeric value (float) is NOT an edit
    edits = diffSheet(base, { 0: { 0: { v: 'Revenue' }, 1: { v: 1000 } } })
    assert(edits.length === 0, 'identical snapshot produces zero edits')
  } finally {
    await cleanup()
  }
}

async function main() {
  console.log('Round-trip preservation test\n')
  const { mod, cleanup } = await loadTsModule('src/renderer/src/lib/xlsxPatch.ts', '.xlsxPatch.gen.mjs')
  try {
    const original = await buildFixture()
    const zip0 = await JSZip.loadAsync(original)
    const sheetName = Object.keys(zip0.files).find((n) => /xl\/worksheets\/sheet\d+\.xml$/.test(n))
    const ws0 = await partOf(zip0, sheetName)
    const styles0 = await partOf(zip0, 'xl/styles.xml')
    const wbXml0 = await partOf(zip0, 'xl/workbook.xml')
    const sharedStrings0 = await partOf(zip0, 'xl/sharedStrings.xml')
    const commentPart = Object.keys(zip0.files).find((n) => /xl\/(threaded)?comments\d*\.xml$/i.test(n))
    const comments0 = commentPart ? await partOf(zip0, commentPart) : null

    console.log('Fixture parts present:')
    console.log('  worksheet:', sheetName)
    console.log('  styles.xml:', !!styles0, ' sharedStrings.xml:', !!sharedStrings0)
    console.log('  comments part:', commentPart || '(none — ExcelJS layout)')
    console.log('  calcPr fullCalcOnLoad in workbook.xml:', /fullCalcOnLoad="1"/.test(wbXml0 || ''))
    console.log('  formula B3 in worksheet:', /<f>B1-B2<\/f>/.test(ws0 || ''))
    console.log('  merge A5:C5:', /A5:C5/.test(ws0 || ''))
    console.log('  pane (frozen):', /<pane\b/.test(ws0 || ''))
    console.log('')

    // Apply ONE edit: change B2 (Cost) 400 -> 450 via the real patch code.
    const edits = new Map([[ 'P&L', [{ row: 1, col: 1, value: 450 }] ]]) // B2 = row1,col1 (0-based)
    const patched = await mod.patchXlsx(original, edits)

    const zip1 = await JSZip.loadAsync(patched)
    const ws1 = await partOf(zip1, sheetName)
    const styles1 = await partOf(zip1, 'xl/styles.xml')
    const wbXml1 = await partOf(zip1, 'xl/workbook.xml')
    const sharedStrings1 = await partOf(zip1, 'xl/sharedStrings.xml')
    const comments1 = commentPart ? await partOf(zip1, commentPart) : null

    console.log('Assertions:')
    // 1. the edit landed
    assert(/<c r="B2"[^>]*><v>450<\/v><\/c>/.test(ws1 || ''), 'B2 value updated to 450')
    // 2. formula preserved
    assert(/<f>B1-B2<\/f>/.test(ws1 || ''), 'formula B3 (=B1-B2) preserved as a formula')
    // 3. styles.xml byte-identical
    assert(styles0 === styles1, 'styles.xml byte-for-byte identical (fonts/fills/number formats)')
    // 4. number format string intact (in styles)
    assert(/#,##0;\(#,##0\);\\?-/.test(styles1 || ''), 'number format #,##0;(#,##0);- retained')
    // 5. merged cells intact
    assert(/A5:C5/.test(ws1 || ''), 'merged range A5:C5 preserved')
    // 6. frozen pane intact
    assert((ws0.match(/<pane\b[^>]*>/) || [''])[0] === (ws1.match(/<pane\b[^>]*>/) || [''])[0],
      'frozen pane definition preserved')
    // 7. comments part byte-identical (provenance)
    if (commentPart) assert(comments0 === comments1, 'comments part byte-for-byte identical (provenance)')
    else console.log('  SKIP  comments part not emitted by ExcelJS fixture layout')
    // 8. calcPr fullCalcOnLoad preserved
    assert(/fullCalcOnLoad="1"/.test(wbXml1 || ''), 'calcPr/fullCalcOnLoad preserved')
    // 9. workbook.xml (sheet names + order) unchanged
    assert(wbXml0 === wbXml1, 'workbook.xml unchanged (sheet names + order)')
    // 10. sharedStrings unchanged (string edits use inline strings; this numeric edit must not touch it)
    assert(sharedStrings0 === sharedStrings1, 'sharedStrings.xml unchanged')
    // 11. untouched value cell B1 unchanged
    assert(/<c r="B1"[^>]*><v>1000<\/v><\/c>/.test(ws1 || ''), 'untouched cell B1 (1000) unchanged')

    // --- type transition: number cell B1 (1000) -> string, must use inline string ---
    const edits2 = new Map([[ 'P&L', [{ row: 0, col: 1, value: 'Net Sales' }] ]]) // B1 -> string
    const patched2 = await mod.patchXlsx(original, edits2)
    const zip2 = await JSZip.loadAsync(patched2)
    const ws2 = await partOf(zip2, sheetName)
    const ss2 = await partOf(zip2, 'xl/sharedStrings.xml')
    console.log('\nType-transition assertions (number -> string):')
    assert(/<c r="B1"[^>]*t="inlineStr"[^>]*><is><t[^>]*>Net Sales<\/t><\/is><\/c>/.test(ws2 || ''),
      'number->string written as inline string')
    assert(!/<c r="B1"[^>]*>(?:(?!<\/c>)[\s\S])*<v>1000<\/v>/.test(ws2 || ''),
      'old numeric <v>1000</v> dropped from B1')
    assert(sharedStrings0 === ss2, 'sharedStrings.xml still unchanged after a string edit (inline strings)')

    // --- type transition: string cell A1 ("Revenue", shared string) -> number ---
    const a1Match = /<c r="A1"([^>]*?)(\/>|>[\s\S]*?<\/c>)/.exec(ws0 || '')
    const a1WasShared = / t="s"/.test(a1Match?.[1] || '') || / t="s"/.test(a1Match?.[0] || '')
    console.log(`\nType-transition assertions (string -> number)  [A1 was shared string: ${a1WasShared}]:`)
    const edits3 = new Map([[ 'P&L', [{ row: 0, col: 0, value: 999 }] ]]) // A1 -> number
    const patched3 = await mod.patchXlsx(original, edits3)
    const zip3 = await JSZip.loadAsync(patched3)
    const ws3 = await partOf(zip3, sheetName)
    const a1New = /<c r="A1"[^>]*?(?:\/>|>[\s\S]*?<\/c>)/.exec(ws3 || '')?.[0] || ''
    assert(/<v>999<\/v>/.test(a1New), 'string->number wrote numeric <v>999</v>')
    assert(!/ t="s"/.test(a1New) && !/t="inlineStr"/.test(a1New), 'string->number dropped the t="s" type attribute')
    assert(!/<f>/.test(a1New), 'no stale <f> on the converted cell')

  } finally {
    await cleanup()
  }
}

/** name -> "xl/worksheets/sheetN.xml" (minimal re-impl for the test harness). */
async function sheetNameToPath(zip) {
  const wb = await partOf(zip, 'xl/workbook.xml')
  const rels = await partOf(zip, 'xl/_rels/workbook.xml.rels')
  const ridToTarget = new Map()
  for (const m of rels.matchAll(/<Relationship\b[^>]*>/g)) {
    const id = /\bId="([^"]+)"/.exec(m[0])?.[1]
    let t = /\bTarget="([^"]+)"/.exec(m[0])?.[1]
    if (id && t) ridToTarget.set(id, 'xl/' + t.replace(/^\/?xl\//, '').replace(/^\//, ''))
  }
  const out = []
  for (const m of wb.matchAll(/<sheet\b[^>]*>/g)) {
    const name = /\bname="([^"]*)"/.exec(m[0])?.[1]
    const rid = /\br:id="([^"]+)"/.exec(m[0])?.[1]
    if (name && rid && ridToTarget.has(rid)) out.push({ name, path: ridToTarget.get(rid) })
  }
  return out
}

/** Find a numeric, non-formula literal cell in a worksheet XML: returns {ref,row,col,val}. */
function findNumericCell(xml) {
  const re =
    /<c r="([A-Z]+)(\d+)"((?:(?!\/>)[^>])*)>(?:<f>[\s\S]*?<\/f>)?<v>(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)<\/v><\/c>/g
  let m
  while ((m = re.exec(xml)) !== null) {
    if (/<f>/.test(m[0])) continue // skip formula cells
    const tm = /\st="([^"]+)"/.exec(m[3])
    if (tm && tm[1] !== 'n') continue // allow numeric (t="n" or none); skip string/bool/str
    const letters = m[1]
    let col = 0
    for (const ch of letters) col = col * 26 + (ch.charCodeAt(0) - 64)
    return { ref: letters + m[2], row: Number(m[2]) - 1, col: col - 1, val: Number(m[4]) }
  }
  return null
}

/** Optional: round-trip a REAL pipeline workbook if one is provided. */
async function testRealFixture(mod) {
  const dir = path.join(ROOT, 'scripts', 'fixtures')
  let file = process.env.FIXTURE_XLSX
  if (!file && existsSync(dir)) {
    const xlsx = (await readdir(dir)).filter((f) => /\.xlsx$/i.test(f) && !f.startsWith('~$'))
    if (xlsx.length) file = path.join(dir, xlsx[0])
  }
  if (!file || !existsSync(file)) {
    console.log('\nReal-workbook fixture: none found (set FIXTURE_XLSX or drop one in scripts/fixtures/) — SKIPPED')
    return
  }
  console.log(`\nReal-workbook round-trip: ${path.basename(file)}`)
  const buf = await readFile(file)
  const original = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength)
  const zip0 = await JSZip.loadAsync(original)
  const allParts = Object.keys(zip0.files).filter((n) => !zip0.files[n].dir)

  // pick the first sheet that has an editable numeric literal cell
  const sheetPaths = await sheetNameToPath(zip0)
  let target = null
  for (const sp of sheetPaths) {
    const xml = await partOf(zip0, sp.path)
    const cell = xml && findNumericCell(xml)
    if (cell) {
      target = { ...sp, ...cell }
      break
    }
  }
  if (!target) {
    console.log('  (no editable numeric cell found to edit) — SKIPPED')
    return
  }
  const newVal = target.val + 123
  console.log(`  editing ${target.name}!${target.ref}: ${target.val} -> ${newVal}`)

  const patched = await mod.patchXlsx(
    original,
    new Map([[target.name, [{ row: target.row, col: target.col, value: newVal }]]])
  )
  const zip1 = await JSZip.loadAsync(patched)

  // every part EXCEPT the edited worksheet must be byte-identical; none added/removed
  const allParts1 = Object.keys(zip1.files).filter((n) => !zip1.files[n].dir)
  assert(allParts.length === allParts1.length && allParts.every((p) => allParts1.includes(p)),
    'no parts added or removed (styles, comments, calcChain, sharedStrings, all sheets present)')
  let changedParts = 0
  let commentParts = 0
  for (const p of allParts) {
    const a = await partOf(zip0, p)
    const b = await partOf(zip1, p)
    if (/xl\/(comments\/comment\d+|(threaded)?comments\d*|threadedComments\/threadedComment\d+)\.xml$/i.test(p)) {
      commentParts++
      assert(a === b, `comment part ${p} byte-identical (provenance preserved)`)
    } else if (p === target.path) {
      assert(a !== b, `edited worksheet ${p} changed`)
    } else if (a !== b) {
      changedParts++
      console.error(`    UNEXPECTED change in ${p}`)
    }
  }
  assert(changedParts === 0, 'no unexpected part changed (styles.xml, workbook.xml, other sheets all intact)')
  if (!commentParts) console.log('  (workbook had no comment parts)')
  const wsNew = await partOf(zip1, target.path)
  assert(new RegExp(`<c r="${target.ref}"[^>]*><v>${newVal}</v></c>`).test(wsNew || ''),
    'edit applied at the target cell')
  // formulas in the edited sheet survive
  const fBefore = (await partOf(zip0, target.path)).match(/<f[ >]/g)?.length || 0
  const fAfter = (wsNew.match(/<f[ >]/g) || []).length
  assert(fAfter === fBefore, `formulas in edited sheet preserved (${fBefore})`)
}

async function run() {
  await main()
  await testDiff()
  const { mod, cleanup } = await loadTsModule('src/renderer/src/lib/xlsxPatch.ts', '.xlsxPatch.real.gen.mjs')
  try {
    await testRealFixture(mod)
  } finally {
    await cleanup()
  }
  console.log('')
  if (failures) {
    console.error(`RESULT: ${failures} assertion(s) FAILED`)
    process.exitCode = 1
  } else {
    console.log('RESULT: all assertions passed — round-trip is lossless')
  }
}

run().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
