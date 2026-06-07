import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useApp, pickSourceEntry } from '@/store'

type SecTarget = { file: string; page: number }

// shared chip styling (used by both the visible chips and the hidden measuring copies)
const CHIP_CLASS =
  'shrink-0 whitespace-nowrap rounded px-1 py-0.5 border border-line bg-panel2 ' +
  'hover:border-accent/60 hover:text-accent transition-colors'
const GAP = 8 // px, matches gap-2 in the sync row

/** Thin toolbar above the grid: company/session info, validation badge, and the
 *  sheet→PDF source-sync controls (toggle + linked-source badge + jump chips). */
export function SheetToolbar() {
  const {
    session, sheets, workbook, validation, pdfPaths, sheetSources, activeSheet, activePdf,
    syncPdfToSheet, setSyncPdfToSheet, focusSheetSource
  } = useApp()

  const flagged = (validation?.withheld ?? 0) + (validation?.quarantined ?? 0)

  // The sync feature is active only when we have PDFs AND lineage survived for this job.
  const hasLineage = pdfPaths.length > 0 && Object.keys(sheetSources).length > 0
  const entries = (activeSheet && sheetSources[activeSheet]) || []
  // the entry the viewer is synced to — prefers the open PDF, so badge ⇄ viewer agree
  const primary = pickSourceEntry(entries, activePdf)

  // Secondary jump targets: every (file, page) except the synced entry's first page,
  // ordered high → low by (report, page) — newest report first, then page ascending.
  const secondary: SecTarget[] = []
  entries.forEach((e) =>
    e.pages.forEach((pg, pi) => {
      if (e === primary && pi === 0) return
      secondary.push({ file: e.report_file, page: pg })
    })
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

interface SyncLayout {
  showToggle: boolean
  showBadge: boolean
  nChips: number
  showDropdown: boolean
}

/**
 * Sheet→PDF sync controls with priority-based responsive collapse. With ample width the
 * toggle, source badge, and all jump chips show. As width shrinks, pieces drop out in
 * ascending priority — chips collapse into the dropdown first, then the toggle, then the
 * dropdown, and finally the badge — so only the workbook name (in the parent) always
 * survives. Re-measured on every resize.
 */
function SyncControls({
  syncOn,
  onToggle,
  primary,
  onFocusPrimary,
  secondary,
  onPick
}: {
  syncOn: boolean
  onToggle: () => void
  primary: { report_file: string; pages: number[] } | null
  onFocusPrimary: () => void
  secondary: SecTarget[]
  onPick: (t: SecTarget) => void
}) {
  const rowRef = useRef<HTMLDivElement>(null)
  const meas = useRef<Record<string, HTMLElement | null>>({})
  const chipsMeasRef = useRef<HTMLDivElement>(null)
  const [lay, setLay] = useState<SyncLayout>({
    showToggle: true,
    showBadge: true,
    nChips: secondary.length,
    showDropdown: false
  })

  useLayoutEffect(() => {
    const row = rowRef.current
    if (!row) return
    const compute = () => {
      const W = row.clientWidth - 4 // small slack so content never reaches the exact edge
      const wToggle = meas.current.toggle?.offsetWidth ?? 0
      const wBadge = meas.current.badge?.offsetWidth ?? 0
      const wDrop = meas.current.drop?.offsetWidth ?? 0
      const chipW = Array.from(chipsMeasRef.current?.children ?? []).map(
        (c) => (c as HTMLElement).offsetWidth
      )
      const sum = (arr: number[]) => arr.reduce((s, w) => s + w + GAP, 0)

      // fast path: everything fits → show it all, no dropdown
      if (wBadge + GAP + wToggle + GAP + sum(chipW) <= W) {
        setLay({ showToggle: true, showBadge: true, nChips: secondary.length, showDropdown: false })
        return
      }

      // priority inclusion (keep longest → drop last): badge > dropdown > toggle > chips
      let used = 0
      const place = (w: number) => {
        const need = (used > 0 ? GAP : 0) + w
        if (used + need <= W) {
          used += need
          return true
        }
        return false
      }
      const showBadge = place(wBadge)
      const reserveDropdown = secondary.length > 0 ? place(wDrop) : false
      const showToggle = place(wToggle)
      let nChips = 0
      for (const w of chipW) {
        if (place(w)) nChips++
        else break
      }
      const showDropdown = reserveDropdown && nChips < secondary.length
      setLay({ showToggle, showBadge, nChips, showDropdown })
    }
    compute()
    const ro = new ResizeObserver(compute)
    ro.observe(row)
    return () => ro.disconnect()
  }, [secondary, primary, syncOn])

  const setRef = (key: string) => (el: HTMLElement | null) => {
    meas.current[key] = el
  }

  // ── reusable element renderers (same markup for measuring + real render) ──
  const toggle = (ref?: (el: HTMLElement | null) => void) => (
    <button
      ref={ref as React.Ref<HTMLButtonElement>}
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
  )

  const badge = (ref?: (el: HTMLElement | null) => void) =>
    primary ? (
      <button
        ref={ref as React.Ref<HTMLButtonElement>}
        type="button"
        onClick={onFocusPrimary}
        title={`Open ${primary.report_file} at page ${primary.pages[0]}`}
        className="shrink-0 rounded px-1.5 py-0.5 border border-line bg-panel2 text-ink hover:border-accent/60 hover:text-accent transition-colors whitespace-nowrap"
      >
        📄 {primary.report_file} p.{primary.pages[0]}
      </button>
    ) : (
      <span
        ref={ref as React.Ref<HTMLSpanElement>}
        title="This sheet has no linked source page in the extracted PDFs"
        className="shrink-0 rounded px-1.5 py-0.5 border border-line text-muted/60 whitespace-nowrap"
      >
        no source page
      </span>
    )

  return (
    <div
      ref={rowRef}
      className="flex items-center gap-2 pl-1 border-l border-line flex-1 min-w-0 overflow-hidden"
    >
      {/* hidden measuring copies — natural widths, never affect layout */}
      <div className="absolute w-0 h-0 overflow-hidden" aria-hidden>
        {toggle(setRef('toggle'))}
        {badge(setRef('badge'))}
        <span ref={setRef('drop')} className={CHIP_CLASS}>
          +{secondary.length} ▾
        </span>
        <div ref={chipsMeasRef} className="flex">
          {secondary.map((t, i) => (
            <span key={`m${i}`} className={CHIP_CLASS}>
              {t.file} p.{t.page}
            </span>
          ))}
        </div>
      </div>

      {lay.showToggle && toggle()}
      {lay.showBadge && badge()}
      {secondary.slice(0, lay.nChips).map((t, i) => (
        <button
          key={`${t.file}:${t.page}:${i}`}
          type="button"
          onClick={() => onPick(t)}
          title={`Open ${t.file} at page ${t.page}`}
          className={CHIP_CLASS}
        >
          {t.file} p.{t.page}
        </button>
      ))}
      {lay.showDropdown && <OverflowDropdown items={secondary.slice(lay.nChips)} onPick={onPick} />}
    </div>
  )
}

/** Collapses overflow "also on:" targets into a dropdown. The menu is portaled to <body>
 *  with fixed positioning clamped to the viewport, so it can never render off-screen even
 *  when the trigger sits at (or past) the right edge of the toolbar. */
function OverflowDropdown({
  items,
  onPick
}: {
  items: { file: string; page: number }[]
  onPick: (t: { file: string; page: number }) => void
}) {
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null)
  const btnRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  function place() {
    const b = btnRef.current?.getBoundingClientRect()
    if (!b) return
    // anchor the menu's right edge to the button, but never closer than 8px to the
    // viewport edge — so it stays fully on-screen regardless of the button's position.
    setPos({ top: b.bottom + 4, right: Math.max(8, window.innerWidth - b.right) })
  }

  useEffect(() => {
    if (!open) return
    place()
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (btnRef.current?.contains(t) || menuRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    const onShift = () => setOpen(false) // scroll/resize → re-anchoring is moot; just close
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', onShift)
    window.addEventListener('scroll', onShift, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', onShift)
      window.removeEventListener('scroll', onShift, true)
    }
  }, [open])

  return (
    <div className="relative shrink-0">
      <button
        ref={btnRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={`${items.length} more source page${items.length === 1 ? '' : 's'}`}
        className="rounded px-1 py-0.5 border border-line bg-panel2 hover:border-accent/60 hover:text-accent transition-colors"
      >
        +{items.length} ▾
      </button>
      {open &&
        pos &&
        createPortal(
          <div
            ref={menuRef}
            style={{ position: 'fixed', top: pos.top, right: pos.right, zIndex: 1000 }}
            className="max-w-[min(360px,90vw)] min-w-[150px] max-h-60 overflow-auto rounded border border-line bg-panel2 shadow-lg py-1 text-xs"
          >
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
          </div>,
          document.body
        )}
    </div>
  )
}
