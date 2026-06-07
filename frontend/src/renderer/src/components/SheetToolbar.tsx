import { useApp } from '@/store'

/** Thin toolbar above the grid: company/session info + extraction validation badge. */
export function SheetToolbar() {
  const { session, sheets, workbook, validation } = useApp()
  const flagged = (validation?.withheld ?? 0) + (validation?.quarantined ?? 0)
  return (
    <div className="h-9 shrink-0 flex items-center gap-3 px-3 border-b border-line bg-panel text-xs">
      <span className="font-medium text-ink">{session?.company ?? 'Workbook'}</span>
      {session?.years?.length ? (
        <span className="text-muted">
          {session.years[0]}–{session.years[session.years.length - 1]}
        </span>
      ) : null}
      <span className="text-muted">· {sheets.length} sheets</span>
      {workbook.origin === 'ocr' && validation && (
        <span
          title="Withheld + quarantined values from extraction (see the Validation Ledger sheet)"
          className={
            'rounded px-1.5 py-0.5 border ' +
            (flagged > 0
              ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
              : 'text-green-400 border-green-500/40 bg-green-500/10')
          }
        >
          {flagged > 0 ? `${flagged} flagged` : 'validation clean'}
        </span>
      )}
      <div className="flex-1" />
    </div>
  )
}
