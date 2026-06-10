import { useEffect, useMemo, useRef, useState } from 'react'
import { useApp } from '@/store'
import { FilterDropdown } from './Dashboard'
import {
  ArrowGlyph,
  CrossIcon,
  SadFileIcon,
  SadSearchIcon,
  SearchIcon
} from './HistoryIcons'
import {
  buildHistory,
  applyFilters,
  effectiveSheet,
  TYPE_LABELS,
  type HistEntry
} from '@/lib/historyView'

const APP_NAME = 'AI Financial Intelligence'
const ALL_TYPES = Object.values(TYPE_LABELS)
const FILTER_BUTTON_CLASS = 'h-11 w-full'
const FILTER_CONTROL_CLASS =
  'h-11 w-full rounded-lg border border-line bg-panel2 px-3 text-sm text-ink outline-none focus:border-accent/40'

function chipText(raw: string, ts: number | null): string {
  if (ts == null) return raw || '—'
  return new Date(ts).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  })
}

function Tag({ saved }: { saved: boolean }): JSX.Element {
  return (
    <span
      className={
        'shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ' +
        (saved ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300')
      }
    >
      {saved ? 'saved' : 'unsaved'}
    </span>
  )
}

/** One change row: a natural-language sentence (+ saved/unsaved tag), then the compact
 *  "sheet/cell: old → new" line (with the shared arrow). Workbook-load rows are a single line. */
function Row({ e }: { e: HistEntry }): JSX.Element {
  const chip = chipText(e.tsRaw, e.ts)
  const vsheet = e.verifiedSheet
  const vcell = e.verifiedCell || e.cell

  let sentence: string
  let compact: JSX.Element | null = null
  if (e.type === 'load') {
    sentence = `Workbook loaded into ${APP_NAME}.`
  } else if (e.type === 'verify' || e.type === 'unverify') {
    const mark = e.type === 'verify' ? 'verified' : 'unverified'
    sentence = `Cell ${vcell} manually marked as ${mark}${vsheet ? ` in the "${vsheet}" sheet` : ''}.`
    compact = (
      <>
        <span className="text-ink">{`${vsheet || e.sheet}/${vcell}`}:</span>
        <span>{e.type === 'verify' ? 'unverified' : 'verified'}</span>
        <ArrowGlyph className="mx-1 align-[-1px] text-muted" />
        <span>manually {mark}</span>
      </>
    )
  } else {
    sentence = `Cell ${e.cell} value changed from ${e.old || '(blank)'} to ${e.new || '(blank)'} in the "${e.sheet}" sheet.`
    compact = (
      <>
        <span className="text-ink">{`${e.sheet}/${e.cell}`}:</span>
        <span>{e.old || '(blank)'}</span>
        <ArrowGlyph className="mx-1 align-[-1px] text-muted" />
        <span>{e.new || '(blank)'}</span>
      </>
    )
  }

  return (
    <li className="rounded-lg border border-line bg-panel px-3 py-2.5 space-y-2.5">
      <div className="flex items-start gap-2.5 text-[13px] leading-snug">
        <span className="shrink-0 rounded bg-panel2 px-1.5 py-0.5 text-[11px] tabular-nums text-muted">
          {chip}
        </span>
        <span className="flex-1">{sentence}</span>
        {e.type !== 'load' && <Tag saved={e.saved} />}
      </div>
      {compact && (
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 pl-1 text-xs leading-snug text-muted">
          {compact}
        </div>
      )}
    </li>
  )
}

function EmptyState({
  icon,
  title,
  hint
}: {
  icon: JSX.Element
  title: string
  hint: string
}): JSX.Element {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center text-muted">
      <div className="h-16 w-16 opacity-70">{icon}</div>
      <div className="text-sm font-medium text-ink/80">{title}</div>
      <div className="text-xs">{hint}</div>
    </div>
  )
}

export function HistoryPanel(): JSX.Element {
  const sheets = useApp((s) => s.sheets)
  const editTimes = useApp((s) => s.editTimes)
  const sessionStart = useApp((s) => s.sessionStart)
  const validationLedger = useApp((s) => s.validationLedger)
  const dirty = useApp((s) => s.workbook.dirty)
  const cleanToken = useApp((s) => s.cleanToken)
  const loadSeq = useApp((s) => s.loadSeq)
  const editSeq = useApp((s) => s.editSeq)
  const toast = useApp((s) => s.toast)
  const setPanel = useApp((s) => s.setPanel)

  // Live edits live in the Univer snapshot (read imperatively by buildHistory), not in React
  // state — so a per-edit editSeq tick is what drives a recompute. Debounce it (~200ms) so a
  // burst of keystrokes coalesces into one refresh instead of thrashing on every cell write.
  const [editTick, setEditTick] = useState(0)
  useEffect(() => {
    const t = setTimeout(() => setEditTick(editSeq), 200)
    return () => clearTimeout(t)
  }, [editSeq])

  const excludeCells = useMemo(
    () =>
      validationLedger?.mvCell
        ? new Set([`${validationLedger.ledgerSheetName}!${validationLedger.mvCell}1`])
        : undefined,
    [validationLedger]
  )

  // Full change list — recompute when the workbook / edits / save state change.
  const all = useMemo(
    () => buildHistory(sheets, editTimes, sessionStart ?? '', excludeCells),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sheets, editTimes, sessionStart, excludeCells, dirty, cleanToken, loadSeq, editTick]
  )

  const sheetOptions = useMemo(
    () => Array.from(new Set(all.map(effectiveSheet).filter(Boolean) as string[])).sort(),
    [all]
  )

  // filters (defaults: all types + all sheets, no date, no search)
  const [search, setSearch] = useState('')
  const [debounced, setDebounced] = useState('')
  const [types, setTypes] = useState<string[]>(ALL_TYPES)
  const [selSheets, setSelSheets] = useState<string[]>([])
  const [date, setDate] = useState('')

  // seed/extend the sheet selection as options appear (new sheets default to selected)
  const seededSheets = useRef<string[]>([])
  useEffect(() => {
    const fresh = sheetOptions.filter((s) => !seededSheets.current.includes(s))
    if (fresh.length) {
      setSelSheets((prev) => Array.from(new Set([...prev, ...fresh])))
      seededSheets.current = [...seededSheets.current, ...fresh]
    }
  }, [sheetOptions])

  // 300ms debounced search
  useEffect(() => {
    const t = setTimeout(() => setDebounced(search), 300)
    return () => clearTimeout(t)
  }, [search])

  const shown = useMemo(
    () =>
      applyFilters(all, {
        search: debounced,
        types,
        sheets: selSheets.length ? selSheets : sheetOptions,
        date: date || null
      }),
    [all, debounced, types, selSheets, sheetOptions, date]
  )

  const reset = (): void => {
    setSearch('')
    setDebounced('')
    setTypes(ALL_TYPES)
    setSelSheets(sheetOptions)
    setDate('')
  }

  return (
    <div className="flex h-full flex-col bg-panel2 text-ink">
      {/* header */}
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <span className="text-sm font-medium">History</span>
        <button
          onClick={() => setPanel('history', false)}
          className="rounded p-1 text-muted hover:bg-line hover:text-ink"
          title="Close history"
        >
          <CrossIcon className="h-3 w-3" />
        </button>
      </div>

      {/* search */}
      <div className="px-3 pt-2.5">
        <div className="relative">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search sheet, cell, value…"
            className="w-full rounded-md border border-line bg-panel py-1.5 pl-8 pr-8 text-sm text-ink placeholder:text-muted outline-none focus:border-accent/50"
          />
          {search && (
            <button
              onClick={() => setSearch('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted hover:text-ink"
              title="Clear search"
            >
              <CrossIcon className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>

      {/* filters */}
      <div className="grid grid-cols-2 gap-2 border-b border-line px-3 py-2.5">
        <FilterDropdown
          label="Type"
          options={ALL_TYPES}
          selected={types}
          onChange={setTypes}
          toast={toast}
          explicit
          buttonClassName={FILTER_BUTTON_CLASS}
        />
        <FilterDropdown
          label="Sheet"
          options={sheetOptions}
          selected={selSheets}
          onChange={setSelSheets}
          toast={toast}
          explicit
          buttonClassName={FILTER_BUTTON_CLASS}
        />
        <input
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
          className={`${FILTER_CONTROL_CLASS} [color-scheme:dark]`}
          title="Filter by day"
        />
        <button
          onClick={reset}
          className={`${FILTER_CONTROL_CLASS} text-left text-muted hover:border-accent/40 hover:text-ink`}
        >
          Reset
        </button>
      </div>

      {/* list / empty states */}
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2.5">
        {all.length === 0 ? (
          <EmptyState
            icon={<SadFileIcon className="h-full w-full" />}
            title="No history yet"
            hint="Edits you make to the workbook will appear here."
          />
        ) : shown.length === 0 ? (
          <EmptyState
            icon={<SadSearchIcon className="h-full w-full" />}
            title="No changes match"
            hint="Try a different search or adjust the filters."
          />
        ) : (
          <ul className="space-y-2.5">
            {shown.map((e, i) => (
              <Row key={i} e={e} />
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}
