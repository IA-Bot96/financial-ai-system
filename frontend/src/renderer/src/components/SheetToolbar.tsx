import { useApp } from '@/store'

/** Thin toolbar above the grid: company/session info, validation badge, and the
 *  sheet→PDF source-sync controls (toggle + linked-source badge). */
export function SheetToolbar() {
  const {
    session, sheets, workbook, validation,
    pdfPaths, sheetSources, activeSheet, syncPdfToSheet, setSyncPdfToSheet, focusSheetSource
  } = useApp()

  const flagged = (validation?.withheld ?? 0) + (validation?.quarantined ?? 0)

  // The sync feature is active only when we have PDFs AND lineage survived for this job.
  const hasLineage = pdfPaths.length > 0 && Object.keys(sheetSources).length > 0
  const entries = (activeSheet && sheetSources[activeSheet]) || []
  const primary = entries[0] // highest weight = the sheet's primary source

  // Secondary jump targets: every (file, page) except the primary's first page.
  const secondary: { file: string; page: number }[] = []
  entries.forEach((e, ei) =>
    e.pages.forEach((pg, pi) => {
      if (ei === 0 && pi === 0) return
      secondary.push({ file: e.report_file, page: pg })
    })
  )

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

      {/* ── sheet → PDF source sync ── */}
      {hasLineage && (
        <div className="flex items-center gap-2 pl-1 border-l border-line">
          {/* toggle */}
          <button
            type="button"
            role="switch"
            aria-checked={syncPdfToSheet}
            onClick={() => setSyncPdfToSheet(!syncPdfToSheet)}
            title="Automatically scroll the PDF to the active sheet's source page"
            className="flex items-center gap-1.5 text-muted hover:text-ink transition-colors"
          >
            <span
              className={
                'relative inline-block h-3.5 w-6 rounded-full transition-colors ' +
                (syncPdfToSheet ? 'bg-accent' : 'bg-line')
              }
            >
              <span
                className={
                  'absolute top-0.5 h-2.5 w-2.5 rounded-full bg-white transition-all ' +
                  (syncPdfToSheet ? 'left-3' : 'left-0.5')
                }
              />
            </span>
            Sync PDF to sheet
          </button>

          {/* primary source badge (clickable → re-center the PDF) */}
          {primary ? (
            <button
              type="button"
              onClick={() => focusSheetSource(primary)}
              title={`Open ${primary.report_file} at page ${primary.pages[0]}`}
              className="rounded px-1.5 py-0.5 border border-line bg-panel2 text-ink hover:border-accent/60 hover:text-accent transition-colors"
            >
              📄 {primary.report_file} p.{primary.pages[0]}
            </button>
          ) : (
            <span
              title="This sheet has no linked source page in the extracted PDFs"
              className="rounded px-1.5 py-0.5 border border-line text-muted/60"
            >
              no source page
            </span>
          )}

          {/* secondary jump targets */}
          {secondary.length > 0 && (
            <span className="flex items-center gap-1 text-muted">
              also on:
              {secondary.slice(0, 4).map((t, i) => (
                <button
                  key={`${t.file}:${t.page}:${i}`}
                  type="button"
                  onClick={() =>
                    focusSheetSource({ report_file: t.file, pages: [t.page], weight: 0 }, t.page)
                  }
                  title={`Open ${t.file} at page ${t.page}`}
                  className="rounded px-1 py-0.5 border border-line bg-panel2 hover:border-accent/60 hover:text-accent transition-colors"
                >
                  {t.file} p.{t.page}
                </button>
              ))}
              {secondary.length > 4 && <span>+{secondary.length - 4}</span>}
            </span>
          )}
        </div>
      )}

      <div className="flex-1" />
    </div>
  )
}
