import { useEffect, useState, DragEvent } from 'react'
import { useApp } from '@/store'
import { Button } from './ui/Button'
import { Plus, UploadCloud } from './ui/icons'
import pdfIcon from '@/public/icons/pdf.svg'

type Picked = { path: string; name: string; size: number }

const PDF_MAX = 50 * 1024 * 1024
const PDF_MAX_COUNT = 5
const MSG = {
  pdfSize: 'PDF file exceeds the 50 MB upload limit. Please upload a file smaller than 50 MB.',
  pdfSizeMulti:
    'One or more PDF files exceed the 50 MB upload limit. Please upload files smaller than 50 MB.',
  notPdf: 'Only PDF files can be attached here. Please upload PDFs.',
  maxPdf: 'A maximum of 5 PDF files can be attached at a time. Please upload 5 or fewer files.'
}

const ext = (n: string) => n.slice(n.lastIndexOf('.') + 1).toLowerCase()
const isPdf = (n: string) => ext(n) === 'pdf'
const kb = (b: number) =>
  b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`

/**
 * Lightweight "attach PDFs" modal — for workbooks with no extracted source (e.g. an Excel
 * upload). Just collects PDFs to view alongside the sheet; no extraction/template. On
 * confirm it sets pdfPaths and opens the PDF dock, after which the viewer behaves as usual.
 */
export function AttachPdfModal() {
  const { closeAttachPdfs, setPdfPaths, setPanel, toast } = useApp()
  const [drag, setDrag] = useState(false)
  const [pdfs, setPdfs] = useState<Picked[]>([])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && closeAttachPdfs()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [closeAttachPdfs])

  function add(files: Picked[]) {
    if (!files.length) return
    if (files.some((f) => !isPdf(f.name))) return toast('error', MSG.notPdf)
    const over = files.filter((f) => f.size > PDF_MAX)
    if (over.length) return toast('error', files.length === 1 ? MSG.pdfSize : MSG.pdfSizeMulti)
    setPdfs((prev) => {
      // de-dupe by path, cap at the max count
      const byPath = new Map(prev.map((f) => [f.path, f]))
      files.forEach((f) => byPath.set(f.path, f))
      const merged = Array.from(byPath.values())
      if (merged.length > PDF_MAX_COUNT) toast('error', MSG.maxPdf)
      return merged.slice(0, PDF_MAX_COUNT)
    })
  }

  async function onPick() {
    add(await window.api.pickFiles({ extensions: ['pdf'], multi: true }))
  }
  function onDrop(e: DragEvent) {
    e.preventDefault()
    setDrag(false)
    add(
      Array.from(e.dataTransfer.files).map((f) => ({
        path: (f as unknown as { path: string }).path,
        name: f.name,
        size: f.size
      }))
    )
  }
  function removePdf(path: string) {
    setPdfs((prev) => prev.filter((f) => f.path !== path))
  }

  function confirm() {
    if (!pdfs.length) return
    setPdfPaths(pdfs.map((f) => f.path))
    setPanel('pdf', true)
    toast('success', `${pdfs.length} PDF${pdfs.length === 1 ? '' : 's'} added successfully`)
    closeAttachPdfs()
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onClick={closeAttachPdfs}
    >
      <div
        className="w-[560px] rounded-2xl bg-panel border border-line shadow-2xl p-6 flex flex-col"
        style={{ maxHeight: '85vh' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-1 shrink-0">
          <UploadCloud className="w-5 h-5 text-accent" />
          <h2 className="text-base font-semibold">Attach PDFs</h2>
        </div>
        <p className="text-sm text-muted mb-4 shrink-0">
          Add PDFs to view alongside this workbook.
        </p>

        {pdfs.length === 0 ? (
          <div
            onDragOver={(e) => {
              e.preventDefault()
              setDrag(true)
            }}
            onDragLeave={() => setDrag(false)}
            onDrop={onDrop}
            onClick={onPick}
            className={
              'flex-1 min-h-[220px] cursor-pointer rounded-xl border-2 border-dashed ' +
              'flex flex-col items-center justify-center text-center transition-colors ' +
              (drag ? 'border-accent bg-accent/5' : 'border-line hover:border-accent/60')
            }
          >
            <div className="h-20 w-20 rounded-full bg-panel2 border border-line flex items-center justify-center text-accent">
              <Plus className="w-8 h-8" />
            </div>
            <div className="mt-6 text-base">
              <span className="font-semibold">Click to upload PDFs</span> or drag and drop here
            </div>
            <div className="mt-3 flex items-center justify-center gap-2 text-sm text-muted/80">
              <img src={pdfIcon} className="w-6 h-6" alt="" /> Up to {PDF_MAX_COUNT} PDFs, 50 MB each
            </div>
          </div>
        ) : (
          <>
            <div className="flex items-center justify-between mb-2 shrink-0">
              <span className="text-xs font-medium text-muted uppercase tracking-wide">
                PDFs ({pdfs.length}/{PDF_MAX_COUNT})
              </span>
              {pdfs.length < PDF_MAX_COUNT && (
                <button
                  onClick={onPick}
                  className="flex items-center gap-1 text-xs text-accent hover:underline"
                >
                  <Plus className="w-3 h-3" /> Add PDF
                </button>
              )}
            </div>
            <div className="space-y-2 max-h-72 overflow-auto">
              {pdfs.map((f) => (
                <div
                  key={f.path}
                  className="flex items-center gap-3 rounded-lg bg-panel2 border border-line px-3 py-2"
                >
                  <img src={pdfIcon} className="w-7 h-7 shrink-0" alt="" />
                  <div className="flex-1 min-w-0">
                    <div className="truncate text-sm">{f.name}</div>
                    <div className="text-xs text-muted mt-0.5">{kb(f.size)}</div>
                  </div>
                  <button
                    onClick={() => removePdf(f.path)}
                    title="Remove"
                    className="shrink-0 p-1.5 rounded text-muted hover:text-red-400 hover:bg-red-400/10 transition-colors"
                  >
                    <TrashIcon />
                  </button>
                </div>
              ))}
            </div>
          </>
        )}

        <div className="mt-5 flex justify-end gap-2 shrink-0">
          <Button variant="ghost" onClick={closeAttachPdfs}>
            Cancel
          </Button>
          <Button onClick={confirm} disabled={pdfs.length === 0}>
            Open in viewer
          </Button>
        </div>
      </div>
    </div>
  )
}

function TrashIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="15"
      height="15"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </svg>
  )
}
