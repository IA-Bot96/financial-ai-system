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
import { rm } from 'node:fs/promises'
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

/** Transpile src/lib/xlsxPatch.ts -> a temp ESM module and import it. */
async function loadPatchModule() {
  const out = path.join(ROOT, 'scripts', '.xlsxPatch.gen.mjs')
  await build({
    entryPoints: [path.join(ROOT, 'src/renderer/src/lib/xlsxPatch.ts')],
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

async function main() {
  console.log('Round-trip preservation test\n')
  const { mod, cleanup } = await loadPatchModule()
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

    // --- second case: string edit must use inline string and leave sharedStrings alone ---
    const edits2 = new Map([[ 'P&L', [{ row: 0, col: 1, value: 'Net Sales' }] ]]) // B1 -> string
    const patched2 = await mod.patchXlsx(original, edits2)
    const zip2 = await JSZip.loadAsync(patched2)
    const ws2 = await partOf(zip2, sheetName)
    const ss2 = await partOf(zip2, 'xl/sharedStrings.xml')
    console.log('\nString-edit assertions:')
    assert(/<c r="B1"[^>]*t="inlineStr"[^>]*><is><t[^>]*>Net Sales<\/t><\/is><\/c>/.test(ws2 || ''),
      'string edit written as inline string')
    assert(sharedStrings0 === ss2, 'sharedStrings.xml still unchanged after a string edit (inline strings)')

    console.log('')
    if (failures) {
      console.error(`RESULT: ${failures} assertion(s) FAILED`)
      process.exitCode = 1
    } else {
      console.log('RESULT: all assertions passed — round-trip is lossless')
    }
  } finally {
    await cleanup()
  }
}

main().catch((e) => {
  console.error(e)
  process.exitCode = 1
})
