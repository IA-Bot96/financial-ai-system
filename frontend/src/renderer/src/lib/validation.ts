/**
 * Read-only validation overlay derived from the workbook's built-in `Validation Ledger`
 * sheet (written into every pipeline-generated .xlsx). Pure data → no Univer/DOM deps, so it
 * can be built once per workbook load and unit-tested in plain Node.
 *
 * Ledger columns (header row 1, data from row 2):
 *   Status · Sheet · Cell/Label · Metric · Year · Value · Face truth · Source · Note ·
 *   Manually Verified
 * We map headers by name (positional fallback) so a column reorder doesn't silently misread.
 *
 * Nothing here writes to the workbook — the only write in the whole feature is the
 * "Manually Verified" checkbox, which edits the ledger cell via the live grid (see store).
 */
import type { ParsedSheet } from './sheetjs'

// error / warning / minor drive the highlight colour; ok = no highlight.
export type Severity = 'error' | 'warning' | 'minor' | 'ok'
// The colour actually painted: severities + "verified" (green) which overrides severity.
export type ColorKey = 'error' | 'warning' | 'minor' | 'verified'

// Status → severity (compared case-insensitively; the ledger mixes UPPER and lower tokens).
const ERROR_STATUSES = new Set(['MISMATCH', 'NO_FACE_TRUTH', 'UNEVALUATED', 'SIGN'])
const WARNING_STATUSES = new Set(['DETAIL_INCOMPLETE', 'WITHHELD'])
const MINOR_STATUSES = new Set(['DETAIL_PLUG', 'FALLBACK'])
const IDENTITY_FAIL = 'IDENTITY_FAIL'

const LEDGER_SHEET = 'validation ledger'
// Metadata sheets that are never decorated nor listed (not data under review).
const EXCLUDED_SHEETS = new Set([
  'validation ledger', 'source ledger', 'scope & notes', 'insights', 'insights review'
])

const COORD_RE = /^[A-Za-z]{1,3}[0-9]+$/
const TRUTHY_RE = /^(true|1|yes|y|x|✓|verified)$/i

export interface ValidationIssue {
  id: string
  status: string
  severity: Severity            // error | warning | minor (never ok here)
  sheet: string
  cellLabel: string
  cell: string | null           // resolved A1 coordinate, or null (unresolved / identity)
  metric: string
  year: string
  value: string
  faceTruth: string
  source: string
  note: string
  verified: boolean             // Manually Verified column (TRUE = user-confirmed)
  ledgerRow: number             // 0-based row index in the ledger sheet (for the MV write)
}
export interface ValidationData {
  ledgerSheetName: string                                  // real tab name (case preserved)
  mvCell: string                                           // column letters of "Manually Verified" (existing, or the next free column)
  mvNeedsHeader: boolean                                   // true → the header doesn't exist yet; write it into the grid on load
  cellIssue: Record<string, Record<string, ValidationIssue>> // sheet → A1 → issue (highlight + tooltip)
  sheetIssues: Record<string, ValidationIssue[]>           // sheet → coord-resolved issues
  workbookNotes: ValidationIssue[]                         // IDENTITY_FAIL + rows with no resolvable cell coordinate
}

// ── status presentation (friendly, suggesting tone) ─────────────────────────────
const colLetters = (idx: number): string => {
  let s = ''
  let n = idx + 1
  while (n > 0) {
    s = String.fromCharCode(65 + ((n - 1) % 26)) + s
    n = Math.floor((n - 1) / 26)
  }
  return s
}

export function statusSeverity(status: string): Severity {
  const s = status.trim().toUpperCase()
  if (ERROR_STATUSES.has(s)) return 'error'
  if (WARNING_STATUSES.has(s)) return 'warning'
  if (MINOR_STATUSES.has(s)) return 'minor'
  return 'ok' // OK / IDENTITY_OK / unknown
}

/** Format a numeric ledger value with thousands separators, preserving its own decimals.
 *  Non-numeric text (labels, already-formatted strings) is returned unchanged. */
export function fmtNum(raw: string): string {
  const t = (raw ?? '').trim()
  if (!t) return t
  const cleaned = t.replace(/[, ]/g, '')
  if (!/^-?\d+(\.\d+)?$/.test(cleaned)) return t
  const n = Number(cleaned)
  if (!Number.isFinite(n)) return t
  const dot = cleaned.indexOf('.')
  const decimals = dot >= 0 ? cleaned.length - dot - 1 : 0
  return n.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
}

/** " (page N)" parsed from the Source column, else "". */
function pageSuffix(source: string): string {
  const m = /\bp(?:age|g|\.)?\s*(\d+)/i.exec(source || '')
  return m ? ` (page ${m[1]})` : ''
}

// Statuses where the Value/Face-truth columns are populated the other way round.
const SWAP_STATUSES = new Set(['DETAIL_INCOMPLETE', 'DETAIL_PLUG'])

/** got/found per status: DETAIL_* → got=Face truth, found=Value; else got=Value, found=Face truth. */
function gotFound(issue: ValidationIssue): { got: string | null; found: string | null } {
  const swap = SWAP_STATUSES.has(issue.status.trim().toUpperCase())
  const got = (swap ? issue.faceTruth : issue.value) || null
  const found = (swap ? issue.value : issue.faceTruth) || null
  return { got, found }
}

type Present = {
  title: string
  message: string
  color: ColorKey
  got: string | null
  found: string | null
  page: string
  raw?: string // the raw rule/expression (IDENTITY_FAIL), shown only behind a "details" expander
}

/** Plain-English summary for a workbook-level accounting cross-check (IDENTITY_FAIL). The raw
 *  rule (e.g. "cash_at_end = opening + operating + investing + financing") is returned for an
 *  optional "details" expander, never as the headline. */
function identitySummary(issue: ValidationIssue): { message: string; raw: string } {
  const raw = (issue.note || issue.cellLabel || issue.metric || '').trim()
  const lower = raw.toLowerCase()
  const sign = /([A-Za-z][\w .]*?)\s*>=\s*0\b/.exec(raw)
  if (sign || issue.status.trim().toUpperCase() === 'SIGN') {
    const name = (sign?.[1] || issue.metric || 'A value').trim()
    return {
      message: `${name} came through as a negative number, which shouldn't happen — likely a sign or extraction error. Worth checking.`,
      raw
    }
  }
  if (lower.includes('cash') && /(operating|investing|financing|opening)/.test(lower)) {
    return {
      message:
        "Cash-flow figures don't fully reconcile — the cash-flow numbers we extracted may be incomplete or have a sign issue. Worth a look.",
      raw
    }
  }
  return {
    message: `These figures didn't fully add up${issue.metric ? ` for ${issue.metric}` : ''}${issue.year ? ` (${issue.year})` : ''} — worth a look.`,
    raw
  }
}

// status → {title, body}. `m` = metric (already defaulted), `y` = year.
const MSG: Record<string, { title: string; body: (m: string, y: string) => string }> = {
  MISMATCH: {
    title: "Doesn't match the statements",
    body: (m) =>
      `This ${m ? m + ' ' : ''}figure may need a check — it doesn't match what we found in the statements.`
  },
  NO_FACE_TRUTH: {
    title: "Couldn't verify",
    body: (m) =>
      `We couldn't confirm this ${m || 'figure'} — there was no matching figure in the statements to compare against.`
  },
  UNEVALUATED: {
    title: "Couldn't auto-check",
    body: () =>
      "We couldn't automatically check this cell (its formula didn't calculate). Worth confirming."
  },
  IDENTITY_FAIL: {
    title: "Figures don't fully add up",
    body: (m, y) =>
      `A standard cross-check didn't balance${m ? ` for ${m}` : ''}${y ? ` (${y})` : ''} — for example, the parts don't sum to the total. Worth a look.`
  },
  SIGN: {
    title: 'Unexpected negative',
    body: () =>
      'This value is usually positive but came through negative — the sign may have been misread.'
  },
  DETAIL_INCOMPLETE: {
    title: 'Breakdown looks incomplete',
    body: (m) =>
      `The detail rows we captured here add up to much less than the total, so some line items may be missing. The headline ${m || 'figure'} total is unaffected — only this breakdown looks partial.`
  },
  WITHHELD: {
    title: 'Left blank on purpose',
    body: () =>
      "We left this blank because our sources disagreed — better than filling a value we can't stand behind. You may want to enter it manually."
  },
  DETAIL_PLUG: {
    title: 'Minor rounding balanced',
    body: () =>
      'The detail was slightly off the total, so we balanced it. Usually nothing to worry about.'
  },
  FALLBACK: {
    title: 'Best-guess match',
    body: () => 'We matched this with lower confidence — likely fine, but worth a glance.'
  }
}

/** Compose the friendly, suggesting-tone presentation for an issue (tooltip + panel). */
export function presentIssue(issue: ValidationIssue): Present {
  if (issue.verified) {
    return {
      title: 'Verified by you',
      message: "You've confirmed this value. Uncheck to flag it again.",
      color: 'verified',
      got: null,
      found: null,
      page: ''
    }
  }
  const key = issue.status.trim().toUpperCase()
  if (key === 'IDENTITY_FAIL') {
    const { message, raw } = identitySummary(issue)
    return { title: 'Worth double-checking', message, color: 'warning', got: null, found: null, page: '', raw }
  }
  const e = MSG[key]
  const { got, found } = gotFound(issue)
  return {
    title: e ? e.title : 'Worth a glance',
    message: e ? e.body(issue.metric, issue.year) : issue.note || 'This cell was flagged for review.',
    color: issue.severity === 'ok' ? 'minor' : (issue.severity as ColorKey),
    got: got ? fmtNum(got) : null,
    found: found ? fmtNum(found) : null,
    page: pageSuffix(issue.source)
  }
}

export const colorOf = (issue: ValidationIssue): ColorKey =>
  issue.verified ? 'verified' : (issue.severity === 'ok' ? 'minor' : (issue.severity as ColorKey))

// ── derived counts (recomputed from current verified state) ─────────────────────
export interface ReviewCounts {
  errors: number
  warnings: number
  minor: number
  toReview: number // unverified error + warning (the actionable count)
}
export function countSheet(issues: ValidationIssue[]): ReviewCounts {
  const c: ReviewCounts = { errors: 0, warnings: 0, minor: 0, toReview: 0 }
  for (const i of issues) {
    if (i.verified) continue
    if (i.severity === 'error') c.errors++
    else if (i.severity === 'warning') c.warnings++
    else if (i.severity === 'minor') c.minor++
  }
  c.toReview = c.errors + c.warnings
  return c
}
export function totalsOf(data: ValidationData): ReviewCounts {
  const t: ReviewCounts = { errors: 0, warnings: 0, minor: 0, toReview: 0 }
  for (const issues of Object.values(data.sheetIssues)) {
    const c = countSheet(issues)
    t.errors += c.errors
    t.warnings += c.warnings
    t.minor += c.minor
  }
  // workbook-level notes (identity / no-coordinate) are items to review until verified
  const notes = data.workbookNotes.filter((n) => !n.verified).length
  t.toReview = t.errors + t.warnings + notes
  return t
}

/** sheet → A1 → ColorKey, for the render-only highlight (verified overrides severity). */
export function buildColorMap(data: ValidationData): Record<string, Record<string, ColorKey>> {
  const out: Record<string, Record<string, ColorKey>> = {}
  for (const [sheet, byCoord] of Object.entries(data.cellIssue)) {
    const m: Record<string, ColorKey> = {}
    for (const [coord, issue] of Object.entries(byCoord)) m[coord] = colorOf(issue)
    out[sheet] = m
  }
  return out
}

/** Soft tints — keep cell text legible on Univer's white grid. */
export const VALIDATION_BG: Record<ColorKey, string> = {
  error: '#FEE2E2',
  warning: '#FEF3C7',
  minor: '#F1F5F9',
  verified: '#DCFCE7'
}

// ── parse ────────────────────────────────────────────────────────────────────
const cellText = (s: ParsedSheet | undefined, r: number, c: number): string =>
  String(s?.cellData?.[r]?.[c]?.v ?? '').trim()

function headerIndex(ledger: ParsedSheet): Record<string, number> {
  const idx: Record<string, number> = {}
  const row = ledger.cellData?.[0] ?? {}
  for (const c in row) {
    const label = String(row[c]?.v ?? '').trim().toLowerCase()
    if (label && !(label in idx)) idx[label] = Number(c)
  }
  return idx
}

function resolveLabelCoord(sheet: ParsedSheet | undefined, label: string): string | null {
  if (!sheet) return null
  const want = label.trim().toLowerCase()
  if (!want) return null
  for (const rs in sheet.cellData) {
    const t = String(sheet.cellData[rs]?.[0]?.v ?? '').trim().toLowerCase()
    if (t && t === want) return `A${Number(rs) + 1}`
  }
  return null
}

/** Build the validation overlay. Returns null when there's no `Validation Ledger` sheet. */
export function buildValidationData(sheets: ParsedSheet[]): ValidationData | null {
  const ledger = sheets.find((s) => s.name.trim().toLowerCase() === LEDGER_SHEET)
  if (!ledger) return null
  const byName = new Map(sheets.map((s) => [s.name, s]))

  const h = headerIndex(ledger)
  const col = (name: string, fallback: number) => (name in h ? h[name] : fallback)
  const cStatus = col('status', 0)
  const cSheet = col('sheet', 1)
  const cCell = col('cell/label', 2)
  const cMetric = col('metric', 3)
  const cYear = col('year', 4)
  const cValue = col('value', 5)
  const cFace = col('face truth', 6)
  const cSource = col('source', 7)
  const cNote = col('note', 8)
  // Manually Verified column: use the existing one, else the next free column (created on load).
  let mvIdx = 'manually verified' in h ? h['manually verified'] : null
  const mvNeedsHeader = mvIdx == null
  if (mvIdx == null) {
    const headerCols = Object.keys(ledger.cellData?.[0] ?? {}).map(Number)
    mvIdx = (headerCols.length ? Math.max(...headerCols) : 8) + 1
  }

  const data: ValidationData = {
    ledgerSheetName: ledger.name,
    mvCell: colLetters(mvIdx),
    mvNeedsHeader,
    cellIssue: {},
    sheetIssues: {},
    workbookNotes: []
  }

  let n = 0
  for (const rs in ledger.cellData) {
    const r = Number(rs)
    if (r === 0) continue
    const status = cellText(ledger, r, cStatus)
    if (!status) continue
    const sheetName = cellText(ledger, r, cSheet)
    const cellLabel = cellText(ledger, r, cCell)
    const verified = !mvNeedsHeader && TRUTHY_RE.test(cellText(ledger, r, mvIdx))
    const issue: ValidationIssue = {
      id: `v${++n}`,
      status,
      severity: statusSeverity(status),
      sheet: sheetName,
      cellLabel,
      cell: null,
      metric: cellText(ledger, r, cMetric),
      year: cellText(ledger, r, cYear),
      value: cellText(ledger, r, cValue),
      faceTruth: cellText(ledger, r, cFace),
      source: cellText(ledger, r, cSource),
      note: cellText(ledger, r, cNote),
      verified,
      ledgerRow: r
    }

    // IDENTITY_FAIL → workbook-level note (no single cell)
    if (status.trim().toUpperCase() === IDENTITY_FAIL) {
      issue.severity = 'error'
      data.workbookNotes.push(issue)
      continue
    }
    if (issue.severity === 'ok') continue
    if (!sheetName || EXCLUDED_SHEETS.has(sheetName.trim().toLowerCase())) continue

    const coord = COORD_RE.test(cellLabel)
      ? cellLabel.toUpperCase()
      : resolveLabelCoord(byName.get(sheetName), cellLabel)
    issue.cell = coord

    // no resolvable coordinate → workbook-level note (can't highlight/navigate a cell)
    if (!coord) {
      data.workbookNotes.push(issue)
      continue
    }
    ;(data.sheetIssues[sheetName] ||= []).push(issue)
    {
      const byCoord = (data.cellIssue[sheetName] ||= {})
      // a cell with multiple rows keeps the higher-severity issue for its colour/tooltip
      const rank = { ok: 0, minor: 1, warning: 2, error: 3 } as const
      const prev = byCoord[coord]
      if (!prev || rank[issue.severity] > rank[prev.severity]) byCoord[coord] = issue
    }
  }
  return data
}
