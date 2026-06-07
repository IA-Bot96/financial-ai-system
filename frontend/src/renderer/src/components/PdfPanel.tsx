import { useEffect, useMemo, useState } from 'react'
import { Document, Page, pdfjs } from 'react-pdf'
import 'react-pdf/dist/Page/AnnotationLayer.css'
import 'react-pdf/dist/Page/TextLayer.css'
import { useApp } from '@/store'

// pdf.js worker (Vite resolves the URL at build time)
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  'pdfjs-dist/build/pdf.worker.min.mjs',
  import.meta.url
).toString()

const basename = (p: string) => p.replace(/\\/g, '/').split('/').pop() || p

export function PdfPanel() {
  const { pdfPaths, nav } = useApp()
  const [active, setActive] = useState(0)
  const [data, setData] = useState<Uint8Array | null>(null)
  const [numPages, setNumPages] = useState(0)
  const [page, setPage] = useState(1)
  const [scale, setScale] = useState(1.1)

  // load bytes for the active pdf
  useEffect(() => {
    let cancelled = false
    const path = pdfPaths[active]
    if (!path) {
      setData(null)
      return
    }
    window.api.readFile(path).then((buf) => {
      if (!cancelled) {
        setData(new Uint8Array(buf))
        setPage(1)
      }
    })
    return () => {
      cancelled = true
    }
  }, [pdfPaths, active])

  // jump to a page when a citation routes here
  useEffect(() => {
    if (nav.pdfPage && nav.pdfPage >= 1) setPage(nav.pdfPage)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.pdfPage, nav.seq])

  const file = useMemo(() => (data ? { data } : null), [data])

  if (!pdfPaths.length) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-muted">
        No source PDF for this workbook.
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col bg-panel">
      <div className="h-9 shrink-0 flex items-center gap-2 px-2 border-b border-line text-xs">
        {pdfPaths.length > 1 ? (
          <select
            value={active}
            onChange={(e) => setActive(Number(e.target.value))}
            className="bg-panel2 border border-line rounded px-1 py-0.5 max-w-[150px]"
          >
            {pdfPaths.map((p, i) => (
              <option key={p} value={i}>
                {basename(p)}
              </option>
            ))}
          </select>
        ) : (
          <span className="truncate text-muted">{basename(pdfPaths[0])}</span>
        )}
        <div className="flex-1" />
        <button onClick={() => setPage((p) => Math.max(1, p - 1))} className="px-1 hover:text-accent">
          ‹
        </button>
        <span className="text-muted">
          {page}/{numPages || '—'}
        </span>
        <button
          onClick={() => setPage((p) => Math.min(numPages || p, p + 1))}
          className="px-1 hover:text-accent"
        >
          ›
        </button>
        <button onClick={() => setScale((s) => Math.max(0.5, s - 0.15))} className="px-1 hover:text-accent">
          −
        </button>
        <button onClick={() => setScale((s) => Math.min(2.5, s + 0.15))} className="px-1 hover:text-accent">
          +
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-auto flex justify-center p-2">
        {file && (
          <Document
            file={file}
            onLoadSuccess={({ numPages: n }) => setNumPages(n)}
            loading={<div className="text-sm text-muted mt-6">Loading PDF…</div>}
            error={<div className="text-sm text-red-300 mt-6">Failed to render PDF.</div>}
          >
            <Page pageNumber={Math.min(page, numPages || page)} scale={scale} />
          </Document>
        )}
      </div>
    </div>
  )
}
