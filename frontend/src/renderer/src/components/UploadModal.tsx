import { useEffect, useRef, useState, DragEvent } from 'react'
import { useApp, type SessionMeta, type ValidationSummary } from '@/store'
import { parseWorkbook } from '@/lib/sheetjs'
import { Button } from './ui/Button'
import { Plus, UploadCloud } from './ui/icons'
import pdfIcon from '@/public/icons/pdf.svg'
import excelIcon from '@/public/icons/excel.svg'

type Picked = { path: string; name: string; size: number }
type Mode = 'choose' | 'working' | 'extracting' | 'review'

const EXCEL_MAX = 200 * 1024 * 1024
const PDF_MAX = 50 * 1024 * 1024
const PDF_MAX_COUNT = 5
const MSG = {
  excelSize: 'Excel file exceeds the 200 MB upload limit. Please upload a file smaller than 200 MB.',
  pdfSize: 'PDF file exceeds the 50 MB upload limit. Please upload a file smaller than 50 MB.',
  pdfSizeMulti:
    'One or more PDF files exceed the 50 MB upload limit. Please upload files smaller than 50 MB.',
  unsupported: 'Unsupported file type. Please upload PDF or Excel.',
  oneExcel: 'Only one Excel file can be uploaded at a time. Please upload a single file.',
  maxPdf: 'A maximum of 5 PDF files can be uploaded at a time. Please upload 5 or fewer files.',
  generic: 'Something went wrong. Please try again.'
}

interface Job extends ValidationSummary {
  id: string
  status: 'queued' | 'running' | 'done' | 'failed'
  message?: string
}

const ext = (n: string) => n.slice(n.lastIndexOf('.') + 1).toLowerCase()
const isXlsx = (n: string) => ['xlsx', 'xls'].includes(ext(n))
const isPdf = (n: string) => ext(n) === 'pdf'
const kb = (b: number) => (b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`)

export function UploadModal() {
  const { session, closeUpload, loadWorkbook, setPdfPaths, setValidation, setPanel, toast } = useApp()
  const canDismiss = !!session // first load (no workbook) -> modal can't be closed
  const [mode, setMode] = useState<Mode>('choose')
  const [busyName, setBusyName] = useState('')
  const [drag, setDrag] = useState(false)
  const [pdfs, setPdfs] = useState<Picked[]>([])
  const [jobId, setJobId] = useState<string | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const poll = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => () => { if (poll.current) clearInterval(poll.current) }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && canDismiss) closeUpload()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canDismiss, closeUpload])

  function stopPoll() {
    if (poll.current) clearInterval(poll.current)
    poll.current = null
  }
  function dismiss() {
    if (canDismiss) {
      stopPoll()
      closeUpload()
    }
  }
  function back() {
    if (canDismiss) {
      stopPoll()
      closeUpload()
    } else {
      stopPoll()
      setMode('choose')
    }
  }

  function route(files: Picked[]) {
    if (!files.length) return
    const pdfFiles = files.filter((f) => isPdf(f.name))
    const xlsx = files.filter((f) => isXlsx(f.name))
    const other = files.filter((f) => !isPdf(f.name) && !isXlsx(f.name))
    // validation failures stay on the modal + show an error toast (no error screen)
    if (other.length || (pdfFiles.length && xlsx.length)) return toast('error', MSG.unsupported)
    if (xlsx.length) {
      if (xlsx.length > 1) return toast('error', MSG.oneExcel)
      if (xlsx[0].size > EXCEL_MAX) return toast('error', MSG.excelSize)
      return ingestExcel(xlsx[0])
    }
    if (pdfFiles.length > PDF_MAX_COUNT) return toast('error', MSG.maxPdf)
    const over = pdfFiles.filter((f) => f.size > PDF_MAX)
    if (over.length) return toast('error', pdfFiles.length === 1 ? MSG.pdfSize : MSG.pdfSizeMulti)
    startExtraction(pdfFiles)
  }

  async function ingestExcel(f: Picked) {
    setMode('working')
    setBusyName(f.name)
    const res = await window.api.createSession(f.path)
    if (res.status !== 200) {
      console.error('[upload] createSession failed', { status: res.status, body: res.body })
      return fail(MSG.generic)
    }
    try {
      const meta = res.body as SessionMeta
      const sheets = await parseWorkbook(await window.api.readFile(f.path), meta.sheets)
      loadWorkbook(meta, sheets, f.path, 'excel')
      toast('success', 'Excel file uploaded successfully.')
    } catch (e) {
      console.error('[upload] excel parse/render failed', e)
      fail(MSG.generic)
    }
  }

  async function startExtraction(files: Picked[]) {
    setPdfs(files)
    setMode('extracting')
    const res = await window.api.createExtractionJob(files.map((f) => f.path))
    if (res.status !== 200) {
      console.error('[upload] createExtractionJob failed', { status: res.status, body: res.body })
      return fail(MSG.generic)
    }
    const id = (res.body as { job_id: string }).job_id
    setJobId(id)
    poll.current = setInterval(async () => {
      const r = await window.api.request({ method: 'GET', path: `/api/extraction/jobs/${id}` })
      if (r.status !== 200) return
      const j = r.body as Job
      setJob(j)
      if (j.status === 'done') {
        stopPoll()
        setMode('review')
      } else if (j.status === 'failed') {
        stopPoll()
        console.error('[upload] extraction job failed', j.message)
        fail(MSG.generic)
      }
    }, 1500)
  }

  async function review() {
    if (!jobId) return
    setMode('working')
    setBusyName('extracted workbook')
    const res = await window.api.ingestJobResult(jobId)
    if (res.status !== 200 || !res.path) {
      console.error('[upload] ingestJobResult failed', { status: res.status, body: res.body })
      return fail(MSG.generic)
    }
    try {
      const meta = res.body as SessionMeta
      const sheets = await parseWorkbook(await window.api.readFile(res.path), meta.sheets)
      loadWorkbook(meta, sheets, res.path, 'ocr')
      setPdfPaths(pdfs.map((f) => f.path))
      setValidation(job ? pickValidation(job) : null)
      setPanel('pdf', true)
      toast('success', `Extracted ${meta.company}`)
    } catch (e) {
      console.error('[upload] extracted workbook parse/render failed', e)
      fail(MSG.generic)
    }
  }

  // failure during processing: surface a toast and return to the choose state (no error screen)
  function fail(msg: string) {
    stopPoll()
    toast('error', msg)
    setMode('choose')
  }

  async function onPick() {
    route(await window.api.pickFiles({ extensions: ['xlsx', 'xls', 'pdf'], multi: true }))
  }
  function onDrop(e: DragEvent) {
    e.preventDefault()
    setDrag(false)
    route(
      Array.from(e.dataTransfer.files).map((f) => ({
        path: (f as unknown as { path: string }).path,
        name: f.name,
        size: f.size
      }))
    )
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 backdrop-blur-sm"
      onClick={dismiss}
    >
      <div
        className={
          'w-[640px] rounded-2xl bg-panel border border-line shadow-2xl p-6 ' +
          (mode === 'choose' ? 'h-[40vh] min-h-[400px] flex flex-col' : '')
        }
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-1 shrink-0">
          <UploadCloud className="w-5 h-5 text-accent" />
          <h2 className="text-base font-semibold">Upload Files</h2>
        </div>

        {mode === 'choose' && (
          <>
            <p className="text-sm text-muted mb-4 shrink-0">
              Upload your company&apos;s annual report PDFs to extract the financials, or an
              existing Excel workbook to analyze.
            </p>
            <div
              onDragOver={(e) => {
                e.preventDefault()
                setDrag(true)
              }}
              onDragLeave={() => setDrag(false)}
              onDrop={onDrop}
              onClick={onPick}
              className={
                'flex-1 min-h-0 cursor-pointer rounded-xl border-2 border-dashed flex flex-col items-center justify-center text-center transition-colors ' +
                (drag ? 'border-accent bg-accent/5' : 'border-line hover:border-accent/60')
              }
            >
              <div className="h-20 w-20 rounded-full bg-panel2 border border-line flex items-center justify-center text-accent">
                <Plus className="w-8 h-8" />
              </div>
              <div className="mt-6 text-base">
                <span className="font-semibold">Click to upload file</span> or drag and drop file here
              </div>
              <div className="mt-3 flex items-center justify-center gap-4 text-sm text-muted/80">
                <span className="flex items-center gap-2">
                  <img src={pdfIcon} className="w-6 h-6" alt="" /> PDFs → extract
                </span>
                <span className="text-muted/40">•</span>
                <span className="flex items-center gap-2">
                  <img src={excelIcon} className="w-6 h-6" alt="" /> Excel → analyze
                </span>
              </div>
            </div>
          </>
        )}

        {mode === 'working' && (
          <div className="py-8 text-center">
            <div className="mx-auto mb-4 h-6 w-6 rounded-full border-2 border-muted border-t-transparent animate-spin" />
            <div className="text-sm">Analyzing {busyName}…</div>
          </div>
        )}

        {(mode === 'extracting' || mode === 'review') && (
          <>
            <p className="text-sm text-muted mb-3">
              {mode === 'extracting'
                ? 'Extracting financial data from the report(s)…'
                : 'Extraction complete — review and load.'}
            </p>
            <div className="space-y-2 max-h-64 overflow-auto">
              {pdfs.map((f) => (
                <FileRow key={f.path} name={f.name} size={f.size} done={mode === 'review'} />
              ))}
            </div>
            {mode === 'review' && job && <ValidationCard job={job} />}
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="ghost" onClick={back}>
                {canDismiss ? 'Cancel' : 'Back'}
              </Button>
              {mode === 'review' && <Button onClick={review}>Review &amp; load</Button>}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function FileRow({ name, size, done }: { name: string; size: number; done: boolean }) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-panel2 border border-line px-3 py-2">
      <div className="flex-1 min-w-0">
        <div className="truncate text-sm">{name}</div>
        <div className="mt-1 h-1.5 rounded bg-bg overflow-hidden">
          <div className={done ? 'h-full w-full bg-green-500' : 'h-full w-1/3 bg-accent animate-pulse'} />
        </div>
      </div>
      <div className="text-xs text-muted shrink-0 w-16 text-right">{kb(size)}</div>
      <div className={'text-xs shrink-0 w-16 ' + (done ? 'text-green-400' : 'text-muted')}>
        {done ? 'done' : 'processing'}
      </div>
    </div>
  )
}

function ValidationCard({ job }: { job: Job }) {
  const items: [string, unknown][] = [
    ['Production ready', job.production_ready ? 'yes' : 'no'],
    ['Validation failures', job.validation_failures ?? 0],
    ['Withheld', job.withheld ?? 0],
    ['Quarantined', job.quarantined ?? 0]
  ]
  return (
    <div className="mt-3 rounded-lg border border-line bg-panel2 px-3 py-2.5">
      <div className="text-xs text-muted mb-1.5">Extraction quality</div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {items.map(([k, v]) => (
          <div key={k} className="flex justify-between">
            <span className="text-muted">{k}</span>
            <span>{String(v)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function pickValidation(j: Job): ValidationSummary {
  return {
    production_ready: j.production_ready,
    fully_reconciled: j.fully_reconciled,
    validation_failures: j.validation_failures,
    detail_incomplete: j.detail_incomplete,
    withheld: j.withheld,
    quarantined: j.quarantined
  }
}
