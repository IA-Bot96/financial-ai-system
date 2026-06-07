import { useEffect, useMemo, useRef, useState } from 'react'
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
// render pages this far (px) outside the viewport so they're ready before scrolled to
const MOUNT_MARGIN = '1200px 0px'

export function PdfPanel() {
  const { pdfPaths, nav, toast } = useApp()
  const [active, setActive] = useState(0)
  const [data, setData] = useState<Uint8Array | null>(null)
  const [numPages, setNumPages] = useState(0)
  const [currentPage, setCurrentPage] = useState(1)
  const [pageInput, setPageInput] = useState('1')
  const [scale, setScale] = useState(1.1)
  // which page numbers currently have a real <Page> mounted (virtualization window)
  const [visible, setVisible] = useState<Set<number>>(new Set([1, 2, 3]))
  // estimated page height (px) for not-yet-rendered placeholders; refined as pages render
  const [estHeight, setEstHeight] = useState(900)

  const scrollRef = useRef<HTMLDivElement>(null)
  // a page to scroll to once it (and the pages above it) have laid out
  const pendingJump = useRef<number | null>(null)
  // measured height per page → placeholders keep the exact height when a page unmounts,
  // so scrolling past virtualized pages doesn't shift the layout
  const pageHeights = useRef<Map<number, number>>(new Map())

  // ── load bytes for the active pdf ──────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    const path = pdfPaths[active]
    setNumPages(0)
    setCurrentPage(1)
    setVisible(new Set([1, 2, 3]))
    pageHeights.current.clear()
    if (!path) {
      setData(null)
      return
    }
    window.api.readFile(path).then((buf) => {
      if (!cancelled) setData(new Uint8Array(buf))
    })
    return () => {
      cancelled = true
    }
  }, [pdfPaths, active])

  // report the open PDF up to the store so sheet-sync can prefer it (keeps the user in
  // the document they're reading when switching sheets, and re-aligns when they switch PDFs)
  useEffect(() => {
    const name = pdfPaths[active] ? basename(pdfPaths[active]) : null
    useApp.getState().setActivePdf(name)
  }, [pdfPaths, active])

  // ── navigation (citation OR sheet→PDF sync): jump to (report_file, page) ────
  useEffect(() => {
    if (!nav.pdfSeq) return
    let targetIdx = active
    if (nav.pdfFile) {
      const idx = pdfPaths.findIndex(
        (p) => basename(p).toLowerCase() === nav.pdfFile!.toLowerCase()
      )
      if (idx === -1) {
        toast('info', `Source “${nav.pdfFile}” isn’t loaded in this viewer.`)
        return
      }
      targetIdx = idx
    }
    const wanted = nav.pdfPage && nav.pdfPage >= 1 ? nav.pdfPage : null
    if (targetIdx !== active) {
      // switching docs: remember the page; onDocLoad/onPageRender will scroll once laid out
      pendingJump.current = wanted ?? 1
      setActive(targetIdx)
    } else if (wanted) {
      jumpTo(wanted)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.pdfSeq])

  // ── track the page under the viewport centre as the user scrolls ────────────
  useEffect(() => {
    const container = scrollRef.current
    if (!container || !numPages) return
    let raf = 0
    const onScroll = () => {
      cancelAnimationFrame(raf)
      raf = requestAnimationFrame(() => {
        const mid = container.scrollTop + container.clientHeight / 2
        const wraps = Array.from(
          container.querySelectorAll<HTMLElement>('[data-page]')
        )
        let best = currentPage
        for (const el of wraps) {
          const top = el.offsetTop
          if (top > mid) break
          best = Number(el.dataset.page)
          if (mid < top + el.offsetHeight) break
        }
        if (best !== currentPage) setCurrentPage(best)
      })
    }
    container.addEventListener('scroll', onScroll, { passive: true })
    onScroll()
    return () => {
      container.removeEventListener('scroll', onScroll)
      cancelAnimationFrame(raf)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [numPages, active, scale, estHeight])

  // ── mount/unmount pages near the viewport (virtualization) ──────────────────
  useEffect(() => {
    const container = scrollRef.current
    if (!container || !numPages) return
    const obs = new IntersectionObserver(
      (entries) => {
        setVisible((prev) => {
          const next = new Set(prev)
          for (const e of entries) {
            const n = Number((e.target as HTMLElement).dataset.page)
            if (e.isIntersecting) next.add(n)
            else next.delete(n)
          }
          return next
        })
      },
      { root: container, rootMargin: MOUNT_MARGIN, threshold: 0 }
    )
    container.querySelectorAll('[data-page]').forEach((el) => obs.observe(el))
    return () => obs.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [numPages, active])

  // keep the page-number input in sync with the scrolled-to page
  useEffect(() => setPageInput(String(currentPage)), [currentPage])

  // report the page in view up to the store so the toolbar badge shows the real page
  useEffect(() => {
    useApp.getState().setActivePdfPage(numPages ? currentPage : null)
  }, [currentPage, numPages])

  // re-centre on the current page after a zoom change (heights shift)
  useEffect(() => {
    const id = requestAnimationFrame(() => scrollToPage(currentPage, false))
    return () => cancelAnimationFrame(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scale])

  function scrollToPage(n: number, smooth = true) {
    const container = scrollRef.current
    const el = container?.querySelector<HTMLElement>(`[data-page="${n}"]`)
    if (!container || !el) return
    container.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: smooth ? 'smooth' : 'auto' })
  }

  function jumpTo(raw: number) {
    if (raw == null || Number.isNaN(raw)) return
    let p = Math.floor(raw)
    if (numPages && p > numPages) {
      toast(
        'warning',
        `That source points to page ${p}, but this PDF has only ${numPages} page${numPages === 1 ? '' : 's'}.`
      )
      p = numPages
    }
    if (p < 1) p = 1
    setCurrentPage(p)
    setVisible((prev) => (prev.has(p) ? prev : new Set(prev).add(p)))
    pendingJump.current = p
    scrollToPage(p)
  }

  function onDocLoad({ numPages: n }: { numPages: number }) {
    setNumPages(n)
    setVisible(new Set([1, 2, 3].filter((x) => x <= n)))
    if (pendingJump.current) {
      if (pendingJump.current > n) {
        toast(
          'warning',
          `That source points to page ${pendingJump.current}, but this PDF has only ${n} page${n === 1 ? '' : 's'}.`
        )
        pendingJump.current = n
      }
      setVisible(new Set([1, 2, 3, pendingJump.current].filter((x) => x <= n)))
      const target = pendingJump.current
      requestAnimationFrame(() => scrollToPage(target, false))
    }
  }

  // as pages render their real height settles — refine the placeholder estimate and,
  // if a jump is pending, re-scroll so it converges on the correct offset.
  function onPageRender(pn: number) {
    const el = scrollRef.current?.querySelector<HTMLElement>(`[data-page="${pn}"]`)
    if (el && el.offsetHeight) {
      pageHeights.current.set(pn, el.offsetHeight)
      if (Math.abs(el.offsetHeight - estHeight) > 4) setEstHeight(el.offsetHeight)
    }
    if (pendingJump.current != null) {
      scrollToPage(pendingJump.current, false)
      if (pn >= pendingJump.current) pendingJump.current = null
    }
  }

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
        <button
          onClick={() => jumpTo(currentPage - 1)}
          disabled={currentPage <= 1}
          className="px-1 hover:text-accent disabled:opacity-30"
          title="Previous page"
        >
          ‹
        </button>
        <span className="flex items-center gap-1 text-muted">
          <input
            type="number"
            min={1}
            max={numPages || 1}
            value={pageInput}
            onChange={(e) => setPageInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') jumpTo(Number(pageInput))
            }}
            onBlur={() => jumpTo(Number(pageInput))}
            className="w-9 bg-panel2 border border-line rounded px-1 py-0.5 text-center text-ink [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
            title="Go to page"
          />
          / {numPages || '—'}
        </span>
        <button
          onClick={() => jumpTo(currentPage + 1)}
          disabled={!numPages || currentPage >= numPages}
          className="px-1 hover:text-accent disabled:opacity-30"
          title="Next page"
        >
          ›
        </button>
        <button
          onClick={() => setScale((s) => Math.max(0.5, s - 0.15))}
          className="px-1 hover:text-accent"
          title="Zoom out"
        >
          −
        </button>
        <button
          onClick={() => setScale((s) => Math.min(2.5, s + 0.15))}
          className="px-1 hover:text-accent"
          title="Zoom in"
        >
          +
        </button>
      </div>

      <div ref={scrollRef} className="relative flex-1 min-h-0 overflow-auto p-2">
        {file && (
          <Document
            file={file}
            onLoadSuccess={onDocLoad}
            loading={<div className="text-sm text-muted mt-6 text-center">Loading PDF…</div>}
            error={<div className="text-sm text-red-300 mt-6 text-center">Failed to render PDF.</div>}
          >
            {Array.from({ length: numPages }, (_, i) => i + 1).map((pn) => (
              <div
                key={pn}
                data-page={pn}
                className="mx-auto mb-3 w-fit"
                style={
                  visible.has(pn)
                    ? undefined
                    : { height: pageHeights.current.get(pn) ?? estHeight, width: '92%' }
                }
              >
                {visible.has(pn) ? (
                  <div className="shadow-lg bg-white">
                    <Page
                      pageNumber={pn}
                      scale={scale}
                      onRenderSuccess={() => onPageRender(pn)}
                      loading={
                        <div
                          style={{ height: estHeight }}
                          className="w-full bg-panel2 border border-line"
                        />
                      }
                    />
                  </div>
                ) : (
                  <div className="h-full w-full rounded bg-panel2 border border-line flex items-center justify-center text-xs text-muted/50">
                    Page {pn}
                  </div>
                )}
              </div>
            ))}
          </Document>
        )}
      </div>
    </div>
  )
}
