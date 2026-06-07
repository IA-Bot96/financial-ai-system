import { useEffect, useRef, useState } from 'react'
import { useApp, pickSourceEntry } from '@/store'

type SecTarget = { file: string; page: number }

/** Thin toolbar above the grid: company/session info, validation badge, and the
 *  sheet→PDF source-sync controls (toggle + linked-source badge + jump chips). */
export function SheetToolbar() {
  const {
    session, sheets, workbook, validation, pdfPaths, sheetSources, activeSheet, activePdf,
    activePdfPage, syncPdfToSheet, setSyncPdfToSheet, focusSheetSource
  } = useApp()

  const flagged = (validation?.withheld ?? 0) + (validation?.quarantined ?? 0)

  // The sync feature is active only when we have PDFs AND lineage survived for this job.
  const hasLineage = pdfPaths.length > 0 && Object.keys(sheetSources).length > 0
  const entries = (activeSheet && sheetSources[activeSheet]) || []
  // the entry the viewer is synced to — prefers the open PDF, so badge ⇄ viewer agree
  const primary = pickSourceEntry(entries, activePdf)

  // All source jump targets — every (file, page), including the active one (shown on the
  // bar as the badge). Ordered high → low by (report, page): newest report first, then page.
  const secondary: SecTarget[] = []
  entries.forEach((e) =>
    e.pages.forEach((pg) => secondary.push({ file: e.report_file, page: pg }))
  )
  secondary.sort((a, b) => b.file.localeCompare(a.file) || a.page - b.page)

  return (
    <div className="relative z-30 h-9 shrink-0 flex items-center gap-3 px-3 border-b border-line bg-panel text-xs">
      {/* always-visible workbook identity (never collapses) */}
      <span className="font-medium text-ink shrink-0">{session?.company ?? 'Workbook'}</span>
      {session?.years?.length ? (
        <span className="text-muted shrink-0">
          {session.years[0]}–{session.years[session.years.length - 1]}
        </span>
      ) : null}
      <span className="text-muted shrink-0">· {sheets.length} sheets</span>

      {workbook.origin === 'ocr' && validation && (
        <span
          title="Withheld + quarantined values from extraction (see the Validation Ledger sheet)"
          className={
            'shrink-0 rounded px-1.5 py-0.5 border ' +
            (flagged > 0
              ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
              : 'text-green-400 border-green-500/40 bg-green-500/10')
          }
        >
          {flagged > 0 ? `${flagged} flagged` : 'validation clean'}
        </span>
      )}

      {/* ── sheet → PDF source sync (collapses by priority as space tightens) ── */}
      {hasLineage && (
        <SyncControls
          syncOn={syncPdfToSheet}
          onToggle={() => setSyncPdfToSheet(!syncPdfToSheet)}
          primary={primary}
          activePage={activePdfPage}
          onFocusPrimary={() => primary && focusSheetSource(primary)}
          secondary={secondary}
          onPick={(t) =>
            focusSheetSource({ report_file: t.file, pages: [t.page], weight: 0 }, t.page)
          }
        />
      )}
    </div>
  )
}

/**
 * Sheet→PDF sync controls: the toggle, the ACTIVE source badge (the only source shown on
 * the bar), and a dropdown holding every other source page. Fixed set of elements — no
 * responsive measuring — so nothing can overflow the bar.
 */
function SyncControls({
  syncOn,
  onToggle,
  primary,
  activePage,
  onFocusPrimary,
  secondary,
  onPick
}: {
  syncOn: boolean
  onToggle: () => void
  primary: { report_file: string; pages: number[] } | null
  activePage: number | null
  onFocusPrimary: () => void
  secondary: SecTarget[]
  onPick: (t: SecTarget) => void
}) {
  // show the page actually in view when known, else the sheet's primary source page
  const badgePage = activePage ?? primary?.pages[0]
  return (
    <div className="flex items-center gap-2 pl-1 border-l border-line min-w-0">
      {/* toggle */}
      <button
        type="button"
        role="switch"
        aria-checked={syncOn}
        onClick={onToggle}
        title="Automatically scroll the PDF to the active sheet's source page"
        className="shrink-0 flex items-center gap-1.5 text-muted hover:text-ink transition-colors"
      >
        <span
          className={
            'relative inline-block h-3.5 w-6 rounded-full transition-colors ' +
            (syncOn ? 'bg-accent' : 'bg-line')
          }
        >
          <span
            className={
              'absolute top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-all ' +
              (syncOn ? 'left-3' : 'left-0.5')
            }
          />
        </span>
        Sync PDF to sheet
      </button>

      {/* active source badge — the only source shown on the bar (click = re-center PDF) */}
      {primary ? (
        <button
          type="button"
          onClick={onFocusPrimary}
          title={`Open ${primary.report_file} at page ${primary.pages[0]}`}
          className="shrink-0 rounded px-1.5 py-0.5 border border-line bg-panel2 text-ink hover:border-accent/60 hover:text-accent transition-colors whitespace-nowrap"
        >
          📄 {primary.report_file} p.{badgePage}
        </button>
      ) : (
        <span
          title="This sheet has no linked source page in the extracted PDFs"
          className="shrink-0 rounded px-1.5 py-0.5 border border-line text-muted/60 whitespace-nowrap"
        >
          no source page
        </span>
      )}

      {/* every OTHER source page (the active one excluded) lives in the dropdown */}
      {secondary.length > 0 && <OverflowDropdown items={secondary} onPick={onPick} />}
    </div>
  )
}

/** Lists the non-active source pages in a simple dropdown opening below the trigger. */
function OverflowDropdown({
  items,
  onPick
}: {
  items: { file: string; page: number }[]
  onPick: (t: { file: string; page: number }) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={`${items.length} more source page${items.length === 1 ? '' : 's'}`}
        className="rounded px-1 py-0.5 border border-line bg-panel2 hover:border-accent/60 hover:text-accent transition-colors"
      >
        +{items.length} ▾
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1 z-50 min-w-[150px] max-h-72 overflow-auto rounded border border-line bg-panel2 shadow-lg py-1 text-xs">
          {items.map((t, i) => (
            <button
              key={`${t.file}:${t.page}:${i}`}
              type="button"
              onClick={() => {
                onPick(t)
                setOpen(false)
              }}
              title={`Open ${t.file} at page ${t.page}`}
              className="block w-full text-left px-2 py-1 whitespace-nowrap text-ink hover:bg-accent/10 hover:text-accent transition-colors"
            >
              {t.file} p.{t.page}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
