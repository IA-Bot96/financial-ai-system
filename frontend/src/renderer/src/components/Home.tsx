import { useEffect, useState } from 'react'
import { useApp } from '@/store'

const baseName = (p: string) => p.replace(/\\/g, '/').split('/').pop() || p

/** Empty landing surface: the centered "+" opens the upload modal; offers to reopen the
 * last workbook (crash-recovery, from the userData state file). */
export function Home() {
  const openUpload = useApp((s) => s.openUpload)
  const reopenLast = useApp((s) => s.reopenLast)
  const [last, setLast] = useState<string | null>(null)

  useEffect(() => {
    window.api.getLastFile().then(setLast)
  }, [])

  return (
    <div className="h-full w-full flex flex-col items-center justify-center gap-4 bg-bg">
      <button
        onClick={openUpload}
        className="group flex flex-col items-center gap-4 rounded-2xl border-2 border-dashed border-line hover:border-accent/60 px-16 py-14 transition-colors"
      >
        <div className="h-16 w-16 rounded-full border-2 border-line group-hover:border-accent flex items-center justify-center text-4xl text-muted group-hover:text-accent transition-colors">
          +
        </div>
        <div className="text-center">
          <div className="text-base font-semibold">Load financial data</div>
          <div className="mt-1 text-sm text-muted">
            Drop PDFs to extract, or an Excel to analyze.
          </div>
          <div className="mt-3 text-xs text-muted/70">📄 PDFs → extract&nbsp;&nbsp;•&nbsp;&nbsp;📊 Excel → analyze</div>
        </div>
      </button>
      {last && (
        <button
          onClick={reopenLast}
          className="text-xs text-muted hover:text-accent underline underline-offset-2"
        >
          Reopen last: {baseName(last)}
        </button>
      )}
    </div>
  )
}
