import { useEffect, useRef, useState } from 'react'
import { useApp } from '@/store'
import { cn } from '@/lib/util'
import { ChevronDown } from './ui/icons'
import {
  presentIssue,
  countSheet,
  totalsOf,
  type ValidationIssue,
  type ColorKey
} from '@/lib/validation'

const DOT: Record<ColorKey, string> = {
  error: 'bg-red-500',
  warning: 'bg-amber-500',
  minor: 'bg-slate-400',
  verified: 'bg-green-500'
}

/** First resolved cell coordinate for a sheet's issues (for "jump to sheet" chips). */
function firstCoord(issues: ValidationIssue[]): string {
  return issues.find((i) => i.cell)?.cell ?? 'A1'
}

/**
 * Read-only validation overlay controls, shown above the grid when the workbook carries a
 * `Validation Ledger`. Holds the "Show items to review" toggle, per-sheet count chips, and an
 * Items panel that lists (1) a single collapsible/dismissible "whole workbook" group for
 * identity / no-coordinate notes and (2) the active sheet's cell issues. Every row carries a
 * "Manually Verified" checkbox. Counts reflect the live verified state.
 */
export function ValidationBar() {
  const data = useApp((s) => s.validationLedger)
  const enabled = useApp((s) => s.validationEnabled)
  const show = useApp((s) => s.showValidation)
  const setShow = useApp((s) => s.setShowValidation)
  const panelOpen = useApp((s) => s.validationPanelOpen)
  const setPanel = useApp((s) => s.setValidationPanel)
  const dismissed = useApp((s) => s.workbookNotesDismissed)
  const setDismissed = useApp((s) => s.setWorkbookNotesDismissed)
  const activeSheet = useApp((s) => s.activeSheet)
  const selectCell = useApp((s) => s.selectCell)
  const setManualVerified = useApp((s) => s.setManualVerified)
  const ref = useRef<HTMLDivElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const [notesOpen, setNotesOpen] = useState(true)
  const [pickerOpen, setPickerOpen] = useState(false)

  useEffect(() => {
    if (!panelOpen) return
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setPanel(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [panelOpen, setPanel])

  useEffect(() => {
    if (!pickerOpen) return
    const h = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setPickerOpen(false)
    }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [pickerOpen])

  if (!data || !enabled) return null // no ledger, or the review feature disabled in Settings → hide the bar

  const { sheetIssues, workbookNotes } = data
  const totals = totalsOf(data)
  const activeIssues = (activeSheet && sheetIssues[activeSheet]) || []
  const notes = workbookNotes.filter((n) => !n.verified)

  // every sheet that has flagged items, with its live "to review" count (for the sheet picker)
  const sheetList = Object.entries(sheetIssues)
    .map(([name, issues]) => ({ name, n: countSheet(issues).toReview }))
    .sort((a, b) => b.n - a.n || a.name.localeCompare(b.name))
  const activeCount = activeSheet ? countSheet(sheetIssues[activeSheet] ?? []).toReview : 0

  const grouped: [string, ColorKey, ValidationIssue[]][] = [
    ['Errors', 'error', activeIssues.filter((i) => !i.verified && i.severity === 'error')],
    ['Warnings', 'warning', activeIssues.filter((i) => !i.verified && i.severity === 'warning')],
    ['Minor', 'minor', activeIssues.filter((i) => !i.verified && i.severity === 'minor')],
    ['Verified', 'verified', activeIssues.filter((i) => i.verified)]
  ]
  const activeToReview = countSheet(activeIssues).toReview
  const showNotesGroup = notes.length > 0 && !dismissed
  const nothing = activeIssues.length === 0 && (notes.length === 0 || dismissed)

  return (
    <div className="shrink-0 border-b border-line bg-panel text-sm">
      <div className="flex items-center gap-3 px-4 py-1.5">
        <div className="flex items-center gap-3 shrink-0">
          <button
            role="switch"
            aria-checked={show}
            onClick={() => setShow(!show)}
            className="flex items-center gap-2"
            title="Automated suggestions — always confirm against the actual annual reports."
          >
            <span
              className={cn(
                'relative h-4 w-7 rounded-full transition-colors shrink-0',
                show ? 'bg-accent' : 'bg-line'
              )}
            >
              <span
                className={cn(
                  'absolute top-1/2 left-0.5 h-3 w-3 -translate-y-1/2 rounded-full bg-white transition-transform',
                  show && 'translate-x-3'
                )}
              />
            </span>
            <span className="text-xs font-medium text-ink whitespace-nowrap">Show items to review</span>
            <span className="rounded bg-amber-500/15 text-amber-400 text-[9px] font-semibold px-1 py-0.5 tracking-wide">
              BETA
            </span>
          </button>

          <div className="text-xs whitespace-nowrap">
            {totals.toReview > 0 ? (
              <span className="flex items-center gap-1.5 text-muted">
                <span className="h-2 w-2 rounded-full bg-red-500" />
                <span className="text-ink font-medium">{totals.toReview}</span> to review
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-green-400">
                <span className="h-2 w-2 rounded-full bg-green-500" />
                Nothing to review
              </span>
            )}
          </div>
        </div>

        <div className="flex-1" />

        {/* current-sheet picker — shows the active sheet, dropdown lists every sheet + count */}
        <div className="relative shrink-0" ref={pickerRef}>
          <button
            onClick={() => setPickerOpen((o) => !o)}
            className={cn(
              'flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-xs max-w-[260px] transition-colors',
              pickerOpen
                ? 'border-accent/60 bg-accent/10 text-ink'
                : 'border-line bg-panel2 text-muted hover:text-ink hover:border-accent/40'
            )}
            title="Switch sheet"
          >
            <span className="font-medium text-ink truncate">{activeSheet ?? 'Sheet'}</span>
            <span className="text-muted shrink-0">· {activeCount} to review</span>
            <ChevronDown className={cn('w-3.5 h-3.5 shrink-0 transition-transform', pickerOpen && 'rotate-180')} />
          </button>

          {pickerOpen && (
            <div className="absolute right-0 top-full mt-1.5 z-50 w-[280px] max-h-[60vh] overflow-auto rounded-lg border border-line bg-panel shadow-xl shadow-black/40 p-1.5 space-y-0.5">
              {sheetList.length === 0 ? (
                <p className="px-2 py-2 text-xs text-muted">No flagged sheets.</p>
              ) : (
                sheetList.map(({ name, n }) => (
                  <button
                    key={name}
                    onClick={() => {
                      selectCell(name, firstCoord(sheetIssues[name] ?? []))
                      setPickerOpen(false)
                    }}
                    className={cn(
                      'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left transition-colors',
                      name === activeSheet ? 'bg-accent/10 text-ink' : 'hover:bg-line text-muted hover:text-ink'
                    )}
                  >
                    <span className="text-xs font-medium truncate flex-1">{name}</span>
                    <span className={cn('text-[11px] shrink-0', n > 0 ? 'text-ink/80' : 'text-green-400')}>
                      {n > 0 ? `${n} to review` : 'clear'}
                    </span>
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        {/* items dropdown — all issues on the current sheet + the whole-workbook notes */}
        <div className="relative shrink-0" ref={ref}>
          <button
            onClick={() => setPanel(!panelOpen)}
            className={cn(
              'rounded-md border px-2.5 py-1 text-xs font-medium transition-colors',
              panelOpen
                ? 'border-accent/60 bg-accent/10 text-ink'
                : 'border-line bg-panel2 text-muted hover:text-ink hover:border-accent/40'
            )}
          >
            Items to review
            {activeToReview + notes.length > 0 && (
              <span className="ml-1 text-ink">({activeToReview + notes.length})</span>
            )}
          </button>

          {panelOpen && (
            <div className="absolute right-0 top-full mt-1.5 z-50 w-[400px] max-h-[64vh] overflow-auto rounded-lg border border-line bg-panel shadow-xl shadow-black/40">
              {/* whole-workbook notes — shown once, same on every sheet */}
              {showNotesGroup && (
                <div className="border-b border-line bg-amber-500/5">
                  <div className="flex items-center gap-1 px-2.5 py-2">
                    <button
                      onClick={() => setNotesOpen((o) => !o)}
                      className="flex items-center gap-1.5 text-xs font-medium text-amber-300 flex-1 text-left"
                    >
                      <ChevronDown
                        className={cn('w-3.5 h-3.5 transition-transform shrink-0', !notesOpen && '-rotate-90')}
                      />
                      Things to double-check (whole workbook)
                      <span className="text-amber-400/70 font-normal">({notes.length})</span>
                    </button>
                    <button
                      onClick={() => setDismissed(true)}
                      title="Dismiss for this session"
                      className="text-muted hover:text-ink px-1 leading-none"
                    >
                      ✕
                    </button>
                  </div>
                  {notesOpen && (
                    <div className="px-1.5 pb-1.5 space-y-0.5">
                      {notes.map((n) => (
                        <WorkbookNoteRow key={n.id} issue={n} onVerify={(c) => setManualVerified(n, c)} />
                      ))}
                    </div>
                  )}
                </div>
              )}
              {dismissed && notes.length > 0 && (
                <div className="px-3 py-1.5 border-b border-line text-[11px] text-muted">
                  {notes.length} workbook {notes.length === 1 ? 'check' : 'checks'} hidden ·{' '}
                  <button className="text-accent hover:underline" onClick={() => setDismissed(false)}>
                    Show
                  </button>
                </div>
              )}

              {/* active-sheet header + cell issues */}
              <div className="px-3 py-2 border-b border-line text-xs text-muted">
                {activeSheet ? (
                  <>
                    Items on <span className="text-ink font-medium">{activeSheet}</span>
                  </>
                ) : (
                  'Select a sheet'
                )}
              </div>
              {nothing ? (
                <p className="px-3 py-4 text-xs text-muted">Nothing to review.</p>
              ) : activeIssues.length === 0 ? (
                <p className="px-3 py-3 text-xs text-muted">Nothing to review on this sheet.</p>
              ) : (
                <div className="p-1.5 space-y-0.5">
                  {grouped.map(([label, color, list]) =>
                    list.length === 0 ? null : (
                      <div key={label}>
                        <div className="px-2 pt-1.5 pb-1 text-[10px] font-medium uppercase tracking-wide text-muted/70">
                          {label} ({list.length})
                        </div>
                        {list.map((iss) => (
                          <IssueRow
                            key={iss.id}
                            issue={iss}
                            color={color}
                            onOpen={() => selectCell(iss.sheet, iss.cell || 'A1')}
                            onVerify={(checked) => setManualVerified(iss, checked)}
                          />
                        ))}
                      </div>
                    )
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function IssueRow({
  issue,
  color,
  onOpen,
  onVerify
}: {
  issue: ValidationIssue
  color: ColorKey
  onOpen: () => void
  onVerify: (checked: boolean) => void
}) {
  const p = presentIssue(issue)
  const shown = p.got && p.found ? `Shown ${p.got}, we found ${p.found}${p.page}` : null
  return (
    <div className="px-2 py-1.5 rounded-md hover:bg-line transition-colors">
      <div className="flex items-start gap-2">
        <button onClick={onOpen} className="flex-1 min-w-0 text-left" title="Go to cell">
          <div className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full shrink-0 ${DOT[color]}`} />
            <span className="text-xs font-medium text-ink truncate">{p.title}</span>
            <span className="text-[10px] text-muted shrink-0">{issue.cell ?? issue.cellLabel}</span>
          </div>
          <div className="text-[11px] text-muted leading-snug mt-0.5 pl-3.5">{p.message}</div>
          {shown && <div className="text-[11px] text-ink/80 tabular-nums mt-0.5 pl-3.5">{shown}</div>}
        </button>
        <label className="mt-0.5 shrink-0 cursor-pointer" title="Mark as manually verified">
          <input
            type="checkbox"
            checked={issue.verified}
            onChange={(e) => onVerify(e.target.checked)}
            className="h-3.5 w-3.5 accent-green-500 rounded"
          />
        </label>
      </div>
    </div>
  )
}

function WorkbookNoteRow({
  issue,
  onVerify
}: {
  issue: ValidationIssue
  onVerify: (checked: boolean) => void
}) {
  const p = presentIssue(issue)
  const [open, setOpen] = useState(false)
  return (
    <div className="px-2 py-1.5 rounded-md hover:bg-amber-500/5">
      <div className="flex items-start gap-2">
        <span className="mt-1 h-2 w-2 rounded-full bg-amber-500 shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-[11px] text-ink/90 leading-snug">{p.message}</div>
          {p.raw && (
            <>
              <button
                onClick={() => setOpen((o) => !o)}
                className="text-[10px] text-muted hover:text-ink mt-0.5"
              >
                {open ? 'Hide details' : 'Details'}
              </button>
              {open && (
                <div className="text-[10px] text-muted font-mono mt-0.5 break-all bg-panel2 rounded px-1.5 py-1">
                  {p.raw}
                </div>
              )}
            </>
          )}
        </div>
        <label className="mt-0.5 shrink-0 cursor-pointer" title="Mark as manually verified">
          <input
            type="checkbox"
            checked={issue.verified}
            onChange={(e) => onVerify(e.target.checked)}
            className="h-3.5 w-3.5 accent-green-500 rounded"
          />
        </label>
      </div>
    </div>
  )
}
