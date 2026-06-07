import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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

const escapeHtml = (s: string) =>
  s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

type Match = { page: number; indexOnPage: number }
// minimal pdf.js document surface we use for text search
type PdfDoc = {
  numPages: number
  getPage: (n: number) => Promise<{ getTextContent: () => Promise<{ items: { str?: string }[] }> }>
}

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

  // ── search state ──
  const [searchOpen, setSearchOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<Match[]>([])
  const [activeMatch, setActiveMatch] = useState(-1)
  const [searching, setSearching] = useState(false)
  // term highlighted from a cell/citation value (shown when the search bar isn't driving it)
  const [cellHl, setCellHl] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  // a page to scroll to once it (and the pages above it) have laid out
  const pendingJump = useRef<number | null>(null)
  // measured height per page → placeholders keep the exact height when a page unmounts,
  // so scrolling past virtualized pages doesn't shift the layout
  const pageHeights = useRef<Map<number, number>>(new Map())
  // search plumbing
  const pdfDocRef = useRef<PdfDoc | null>(null)
  const textCacheRef = useRef<Map<number, string[]>>(new Map()) // page -> escaped text items
  const searchSeqRef = useRef(0) // ignore stale async search results
  const pendingMatchRef = useRef<Match | null>(null) // match awaiting its page to render
  const matchesRef = useRef<Match[]>([])
  const activeMatchRef = useRef(-1)
  matchesRef.current = matches
  activeMatchRef.current = activeMatch
  // cell/citation highlight plumbing
  const pendingCellRef = useRef<number | null>(null) // page whose first cell-match to focus on render
  // a cell/citation highlight deferred until the (newly-switched) doc loads
  const pendingCellHl = useRef<{ term: string; preferred: number; candidates: number[] } | null>(null)
  const cellSeqRef = useRef(0)

  // ── load bytes for the active pdf ──────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    const path = pdfPaths[active]
    setNumPages(0)
    setCurrentPage(1)
    setVisible(new Set([1, 2, 3]))
    pageHeights.current.clear()
    // reset search for the new document
    pdfDocRef.current = null
    textCacheRef.current.clear()
    setMatches([])
    setActiveMatch(-1)
    pendingMatchRef.current = null
    setCellHl('')
    pendingCellRef.current = null
    pendingCellHl.current = null
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
        const wraps = Array.from(container.querySelectorAll<HTMLElement>('[data-page]'))
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

  // ── search: (re)run as the query/document changes (debounced) ───────────────
  useEffect(() => {
    if (!searchOpen) return
    const t = setTimeout(() => runSearch(query), 300)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, searchOpen, numPages])

  // ── cell/citation → highlight the value on its PDF page ─────────────────────
  useEffect(() => {
    if (!nav.pdfQuerySeq) return
    if (searchOpen) return // manual search owns the highlight while its bar is open
    const term = (nav.pdfQuery ?? '').trim()
    if (!term) {
      setCellHl('')
      pendingCellRef.current = null
      pendingCellHl.current = null
      return
    }
    // a citation carries a target page (pdfQueryPage); a plain cell-select doesn't (uses the
    // current page). Only a citation can be switching documents — a cell-select never is, so
    // don't defer it on a stale nav.pdfFile.
    // Set the highlight term synchronously so pages render WITH marks (renderText matches the
    // value's variants directly). The TARGET page is resolved by searching the sheet's source
    // pages for this PDF and jumping to the first that actually contains the value.
    setCellHl(term)
    const preferred = nav.pdfQueryPage ?? currentPage
    const candidates = nav.pdfQueryPages.length ? nav.pdfQueryPages : [preferred]
    const switching =
      nav.pdfQueryPage != null &&
      !!nav.pdfFile &&
      basename(pdfPaths[active] ?? '').toLowerCase() !== nav.pdfFile.toLowerCase()
    // A citation switching documents must wait for the new doc to load before we can search.
    if (switching) pendingCellHl.current = { term, preferred, candidates }
    else resolveAndFocus(term, candidates, preferred)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.pdfQuerySeq])

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

  function onDocLoad(pdf: PdfDoc) {
    pdfDocRef.current = pdf
    const n = pdf.numPages
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
    // a cell/citation highlight that was waiting for this (newly-switched) doc to load
    if (pendingCellHl.current) {
      const { term, preferred, candidates } = pendingCellHl.current
      pendingCellHl.current = null
      setCellHl(term)
      resolveAndFocus(term, candidates, preferred)
    }
  }

  // as pages render their real height settles — refine the placeholder estimate; if a jump
  // or a search match is awaiting this page, scroll/highlight it now that the layer exists.
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
    // re-apply the active-match highlight on (re)render of its page; scroll only if pending
    const am = matchesRef.current[activeMatchRef.current]
    if (am && am.page === pn) focusMark(am, pendingMatchRef.current?.page === pn)
    // focus a cell/citation highlight that was awaiting this page's text layer
    if (pendingCellRef.current === pn) focusFirstCellMark(pn)
  }

  // ── cell/citation highlight helpers ─────────────────────────────────────────
  // a cell holds a figure like "182,625" or "(2,736)"; the PDF may format it differently,
  // so try a few normalized variants and highlight the first that appears on the page.
  function buildVariants(term: string): string[] {
    const t = term.trim()
    if (!t) return []
    const out = new Set<string>([t])
    const neg = /^\(.*\)$/.test(t)
    const digits = t.replace(/[()]/g, '').replace(/\s+/g, '').replace(/,/g, '')
    if (/^-?\d+(\.\d+)?$/.test(digits)) {
      const n = Math.abs(Number(digits))
      if (Number.isFinite(n)) {
        out.add(n.toLocaleString('en-US')) // grouped: 182,625
        out.add(String(n)) // plain: 182625
        if (neg || Number(digits) < 0) {
          out.add(`(${n.toLocaleString('en-US')})`)
          out.add(`(${n})`)
        }
      }
    }
    return [...out].filter((s) => s.length > 0)
  }

  async function ensurePageText(p: number): Promise<void> {
    if (textCacheRef.current.has(p) || !pdfDocRef.current) return
    try {
      const page = await pdfDocRef.current.getPage(p)
      const tc = await page.getTextContent()
      textCacheRef.current.set(p, tc.items.map((it) => escapeHtml(it.str ?? '')))
    } catch {
      textCacheRef.current.set(p, [])
    }
  }

  function pageHasVariant(page: number, variants: string[]): boolean {
    const items = textCacheRef.current.get(page) ?? []
    return variants.some((v) => {
      const re = new RegExp(escapeRe(escapeHtml(v)), 'i')
      return items.some((s) => re.test(s))
    })
  }

  // Search the sheet's source pages for this PDF and focus the value on the first page that
  // contains it — preferring the citation/current page when it has the value.
  async function resolveAndFocus(term: string, candidates: number[], preferred: number): Promise<void> {
    const doc = pdfDocRef.current
    if (!doc) {
      pendingCellHl.current = { term, preferred, candidates } // run after onDocLoad
      return
    }
    const seq = ++cellSeqRef.current
    const pages = [...new Set([preferred, ...candidates])].filter((p) => p >= 1 && p <= doc.numPages)
    for (const p of pages) {
      await ensurePageText(p)
      if (seq !== cellSeqRef.current) return // superseded by a newer highlight
    }
    const variants = buildVariants(term)
    const order = [preferred, ...pages.filter((p) => p !== preferred)]
    const target = order.find((p) => pageHasVariant(p, variants)) ?? preferred
    focusCellOnPage(target)
  }

  // ensure the page is mounted + scrolled into view, then focus its first highlight once its
  // text layer renders (onRenderTextLayerSuccess) — that's when the <mark>s actually exist.
  function focusCellOnPage(page: number): void {
    const p = Math.max(1, Math.floor(page) || 1)
    setVisible((prev) => (prev.has(p) ? prev : new Set(prev).add(p)))
    pendingCellRef.current = p
    setCurrentPage(p)
    scrollToPage(p)
    requestAnimationFrame(() => focusFirstCellMark(p)) // if it's already rendered
  }

  function focusFirstCellMark(page: number): void {
    const container = scrollRef.current
    if (!container) return
    container.querySelectorAll('mark.pdf-hl-active').forEach((el) => el.classList.remove('pdf-hl-active'))
    const el = container.querySelector<HTMLElement>(`[data-page="${page}"] mark.pdf-hl`)
    if (el) {
      el.classList.add('pdf-hl-active')
      el.scrollIntoView({ block: 'center' })
      pendingCellRef.current = null
    }
  }

  // ── search helpers ──────────────────────────────────────────────────────────
  async function ensureText(): Promise<void> {
    const pdf = pdfDocRef.current
    if (!pdf) return
    for (let p = 1; p <= pdf.numPages; p++) {
      if (textCacheRef.current.has(p)) continue
      try {
        const page = await pdf.getPage(p)
        const tc = await page.getTextContent()
        // store HTML-escaped items so counts line up with what customTextRenderer marks
        textCacheRef.current.set(p, tc.items.map((it) => escapeHtml(it.str ?? '')))
      } catch {
        textCacheRef.current.set(p, [])
      }
    }
  }

  async function runSearch(raw: string): Promise<void> {
    const q = raw.trim()
    if (!q || !pdfDocRef.current) {
      setMatches([])
      setActiveMatch(-1)
      pendingMatchRef.current = null
      return
    }
    const seq = ++searchSeqRef.current
    setSearching(true)
    await ensureText()
    if (seq !== searchSeqRef.current) return // a newer search superseded this one
    const re = new RegExp(escapeRe(escapeHtml(q)), 'gi')
    const list: Match[] = []
    const total = pdfDocRef.current.numPages
    for (let p = 1; p <= total; p++) {
      let k = 0
      for (const s of textCacheRef.current.get(p) ?? []) {
        const found = s.match(re)
        if (found) for (let j = 0; j < found.length; j++) list.push({ page: p, indexOnPage: k++ })
      }
    }
    setSearching(false)
    setMatches(list)
    if (list.length) gotoMatch(0, list)
    else setActiveMatch(-1)
  }

  function focusMark(m: Match, doScroll: boolean) {
    const container = scrollRef.current
    if (!container) return
    container.querySelectorAll('mark.pdf-hl-active').forEach((el) => el.classList.remove('pdf-hl-active'))
    const pageEl = container.querySelector(`[data-page="${m.page}"]`)
    const marks = pageEl?.querySelectorAll<HTMLElement>('mark.pdf-hl')
    const el = marks?.[m.indexOnPage]
    if (el) {
      el.classList.add('pdf-hl-active')
      if (doScroll) el.scrollIntoView({ block: 'center' })
      pendingMatchRef.current = null
    }
  }

  function gotoMatch(i: number, list: Match[] = matches) {
    if (!list.length) return
    const idx = ((i % list.length) + list.length) % list.length
    setActiveMatch(idx)
    const m = list[idx]
    setVisible((prev) => (prev.has(m.page) ? prev : new Set(prev).add(m.page)))
    pendingMatchRef.current = m // onPageRender scrolls once the page's text layer exists
    setCurrentPage(m.page)
    scrollToPage(m.page)
    requestAnimationFrame(() => focusMark(m, true))
  }

  function openSearch() {
    setSearchOpen(true)
    requestAnimationFrame(() => searchInputRef.current?.focus())
  }
  function closeSearch() {
    setSearchOpen(false)
    setQuery('')
    setMatches([])
    setActiveMatch(-1)
    pendingMatchRef.current = null
  }

  // highlight every occurrence of the active term within each text item. The manual search
  // query wins while its bar is open; otherwise the cell/citation value is highlighted.
  const renderText = useCallback(
    ({ str }: { str: string }) => {
      const manual = searchOpen ? query.trim() : ''
      const term = manual || cellHl
      if (!term) return str
      let re: RegExp
      if (manual) {
        re = new RegExp(escapeRe(escapeHtml(manual)), 'gi')
      } else {
        // cell value: match any normalized variant (182,625 / 182625 / (2,736)); numeric
        // boundaries so a figure isn't matched inside a longer number.
        const alts = buildVariants(cellHl).map((v) => escapeRe(escapeHtml(v)))
        if (!alts.length) return str
        re = new RegExp(`(?<![\\d.,])(?:${alts.join('|')})(?![\\d.,])`, 'gi')
      }
      return escapeHtml(str).replace(re, (mm) => `<mark class="pdf-hl">${mm}</mark>`)
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [searchOpen, query, cellHl]
  )

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
      {/* highlight styles for the text-layer <mark>s injected by renderText */}
      <style>{`
        .pdf-hl { background: rgba(255, 213, 0, 0.45); color: inherit; border-radius: 2px; }
        .pdf-hl-active { background: rgba(255, 138, 0, 0.85); }
      `}</style>

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
          onClick={() => (searchOpen ? closeSearch() : openSearch())}
          aria-pressed={searchOpen}
          className={'px-1 hover:text-accent ' + (searchOpen ? 'text-accent' : '')}
          title="Search in PDF"
        >
          <SearchIcon />
        </button>
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

      {/* ── search bar ── */}
      {searchOpen && (
        <div className="h-9 shrink-0 flex items-center gap-2 px-2 border-b border-line bg-panel2 text-xs">
          <SearchIcon className="text-muted shrink-0" />
          <input
            ref={searchInputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') gotoMatch(activeMatch + (e.shiftKey ? -1 : 1))
              else if (e.key === 'Escape') closeSearch()
            }}
            placeholder="Find in document…"
            className="flex-1 min-w-0 bg-panel border border-line rounded px-2 py-0.5 text-ink placeholder:text-muted outline-none focus:border-accent/50"
          />
          <span className="text-muted tabular-nums shrink-0 w-16 text-center">
            {searching ? '…' : matches.length ? `${activeMatch + 1}/${matches.length}` : '0/0'}
          </span>
          <button
            onClick={() => gotoMatch(activeMatch - 1)}
            disabled={!matches.length}
            className="px-1 hover:text-accent disabled:opacity-30"
            title="Previous match (Shift+Enter)"
          >
            ‹
          </button>
          <button
            onClick={() => gotoMatch(activeMatch + 1)}
            disabled={!matches.length}
            className="px-1 hover:text-accent disabled:opacity-30"
            title="Next match (Enter)"
          >
            ›
          </button>
          <button onClick={closeSearch} className="px-1 hover:text-accent" title="Close search">
            ✕
          </button>
        </div>
      )}

      <div ref={scrollRef} className="relative flex-1 min-h-0 overflow-auto p-2">
        {file && (
          <Document
            file={file}
            onLoadSuccess={onDocLoad as never}
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
                      customTextRenderer={renderText}
                      onRenderSuccess={() => onPageRender(pn)}
                      onRenderTextLayerSuccess={() => {
                        // marks live in the text layer — focus the pending highlight here,
                        // where (unlike canvas render) the <mark>s are guaranteed to exist.
                        if (pendingCellRef.current === pn) focusFirstCellMark(pn)
                        const am = matchesRef.current[activeMatchRef.current]
                        if (am && am.page === pn) focusMark(am, pendingMatchRef.current?.page === pn)
                      }}
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

function SearchIcon({ className = '' }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.3-4.3" />
    </svg>
  )
}
