import { useEffect, useRef, useState, DragEvent } from 'react'
import {
  useApp,
  type SessionMeta,
  type ValidationSummary,
  type SheetSources
} from '@/store'
import { parseWorkbook } from '@/lib/sheetjs'
import { Button } from './ui/Button'
import { Plus, UploadCloud } from './ui/icons'
import pdfIcon from '@/public/icons/pdf.svg'
import excelIcon from '@/public/icons/excel.svg'

// ── types ────────────────────────────────────────────────────────────────────

type Picked = { path: string; name: string; size: number }
type Mode = 'choose' | 'stage' | 'working' | 'extracting' | 'review'

/** All pipeline stages in order — used to drive the progress label & bar colour. */
type PipelineStage =
  | 'queued'
  | 'running'
  | 'ingesting'
  | 'ingested'
  | 'detecting_tables'
  | 'extracting'
  | 'extracting_insights'
  | 'interpreted'
  | 'merging'
  | 'mapping'
  | 'validating'
  | 'finalizing'
  | 'done'
  | 'failed'
  | 'cancelled'

interface JobProgress {
  stage: PipelineStage
  pct: number                              // 0–100
  pdfs: Record<string, PipelineStage>      // filename → per-PDF stage
  detail: string | null
  updated_at: number
}

type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
const TERMINAL = new Set<JobStatus>(['done', 'failed', 'cancelled'])

interface Job extends ValidationSummary {
  id: string
  status: JobStatus
  message?: string
  progress?: JobProgress
  sheet_sources?: SheetSources // worksheet → source PDF pages (drives sheet↔PDF sync)
}

// ── constants ────────────────────────────────────────────────────────────────

const EXCEL_MAX = 200 * 1024 * 1024
const PDF_MAX   = 50  * 1024 * 1024
const PDF_MAX_COUNT = 5

const MSG = {
  excelSize:    'Excel file exceeds the 200 MB upload limit. Please upload a file smaller than 200 MB.',
  pdfSize:      'PDF file exceeds the 50 MB upload limit. Please upload a file smaller than 50 MB.',
  pdfSizeMulti: 'One or more PDF files exceed the 50 MB upload limit. Please upload files smaller than 50 MB.',
  unsupported:  'Unsupported file type. Please upload PDF or Excel.',
  oneExcel:     'Only one Excel file can be uploaded at a time. Please upload a single file.',
  maxPdf:       'A maximum of 5 PDF files can be uploaded at a time. Please upload 5 or fewer files.',
  generic:      'Something went wrong. Please try again.'
}

/** Human-readable labels for every pipeline stage. */
const STAGE_LABEL: Record<PipelineStage, string> = {
  queued:               'Queued',
  running:              'Starting…',
  ingesting:            'Reading document',
  ingested:             'Document read',
  detecting_tables:     'Detecting tables',
  extracting:           'Extracting data',
  extracting_insights:  'Extracting insights',
  interpreted:          'Interpreted',
  merging:              'Merging years',
  mapping:              'Mapping template',
  validating:           'Validating',
  finalizing:           'Finalising',
  done:                 'Done',
  failed:               'Failed',
  cancelled:            'Cancelled'
}

// ── helpers ──────────────────────────────────────────────────────────────────

const ext  = (n: string) => n.slice(n.lastIndexOf('.') + 1).toLowerCase()
const isXlsx = (n: string) => ['xlsx', 'xls'].includes(ext(n))
const isPdf  = (n: string) => ext(n) === 'pdf'
const kb = (b: number) =>
  b > 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`

function stageIsDone(s: PipelineStage)      { return s === 'done' || s === 'interpreted' }
function stageIsFailed(s: PipelineStage)    { return s === 'failed' }
function stageIsCancelled(s: PipelineStage) { return s === 'cancelled' }
function stageIsActive(s: PipelineStage)    {
  return !stageIsDone(s) && !stageIsFailed(s) && !stageIsCancelled(s) && s !== 'queued'
}

// ── component ────────────────────────────────────────────────────────────────

export function UploadModal() {
  const {
    session, closeUpload, loadWorkbook, setPdfPaths, setValidation, setSheetSources,
    applySheetSources, setPanel, toast
  } = useApp()
  const canDismiss = !!session

  const [mode,            setMode]            = useState<Mode>('choose')
  const [busyName,        setBusyName]        = useState('')
  const [busySize,        setBusySize]        = useState(0)
  const [drag,            setDrag]            = useState(false)
  const [pdfs,            setPdfs]            = useState<Picked[]>([])
  const [template,        setTemplate]        = useState<Picked | null>(null)
  const [jobId,           setJobId]           = useState<string | null>(null)
  const [job,             setJob]             = useState<Job | null>(null)
  const [showCancelConfirm, setShowCancelConfirm] = useState(false)
  const [cancelling,        setCancelling]        = useState(false)
  const poll = useRef<ReturnType<typeof setInterval> | null>(null)

  // cleanup poll on unmount
  useEffect(() => () => { if (poll.current) clearInterval(poll.current) }, [])

  // Escape key to dismiss (but not while extracting or confirming cancel)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && canDismiss && mode !== 'extracting' && !showCancelConfirm)
        closeUpload()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [canDismiss, closeUpload, mode, showCancelConfirm])

  // ── poll helpers ──────────────────────────────────────────────────────────

  function stopPoll() {
    if (poll.current) clearInterval(poll.current)
    poll.current = null
  }

  // ── navigation ────────────────────────────────────────────────────────────

  function dismiss() {
    if (canDismiss && mode !== 'extracting') { stopPoll(); closeUpload() }
  }
  function back() {
    stopPoll()
    if (mode === 'stage') {
      setPdfs([]); setTemplate(null); setMode('choose')
    } else if (canDismiss) {
      closeUpload()
    } else {
      setMode('choose')
    }
  }

  // ── file picking (stage screen) ───────────────────────────────────────────

  async function addMorePdfs() {
    const picked = await window.api.pickFiles({ extensions: ['pdf'], multi: true })
    if (!picked.length) return
    const over = picked.filter((f) => f.size > PDF_MAX)
    if (over.length) return toast('error', picked.length === 1 ? MSG.pdfSize : MSG.pdfSizeMulti)
    const merged = [...pdfs, ...picked].slice(0, PDF_MAX_COUNT)
    if (pdfs.length + picked.length > PDF_MAX_COUNT) toast('error', MSG.maxPdf)
    setPdfs(merged)
  }

  async function pickTemplate() {
    const picked = await window.api.pickFiles({ extensions: ['xlsx', 'xls'], multi: false })
    if (!picked.length) return
    if (picked[0].size > EXCEL_MAX) return toast('error', MSG.excelSize)
    setTemplate(picked[0])
  }

  function removePdf(path: string) {
    setPdfs((prev) => prev.filter((f) => f.path !== path))
  }

  // ── initial file routing (choose screen) ─────────────────────────────────

  function route(files: Picked[]) {
    if (!files.length) return
    const pdfFiles = files.filter((f) => isPdf(f.name))
    const xlsx     = files.filter((f) => isXlsx(f.name))
    const other    = files.filter((f) => !isPdf(f.name) && !isXlsx(f.name))
    if (other.length || (pdfFiles.length && xlsx.length)) return toast('error', MSG.unsupported)
    if (xlsx.length) {
      if (xlsx.length > 1)            return toast('error', MSG.oneExcel)
      if (xlsx[0].size > EXCEL_MAX)   return toast('error', MSG.excelSize)
      return ingestExcel(xlsx[0])
    }
    if (pdfFiles.length > PDF_MAX_COUNT)                       return toast('error', MSG.maxPdf)
    if (pdfFiles.some((f) => f.size > PDF_MAX))
      return toast('error', pdfFiles.length === 1 ? MSG.pdfSize : MSG.pdfSizeMulti)
    setPdfs(pdfFiles)
    setMode('stage')
  }

  // ── Excel ingest ──────────────────────────────────────────────────────────

  async function ingestExcel(f: Picked) {
    setMode('working')
    setBusyName(f.name)
    setBusySize(f.size)
    const res = await window.api.createSession(f.path)
    if (res.status !== 200) {
      console.error('[upload] createSession failed', { status: res.status, body: res.body })
      return fail(MSG.generic)
    }
    try {
      const meta   = res.body as SessionMeta
      const buf    = await window.api.readFile(f.path)
      const sheets = await parseWorkbook(buf, meta.sheets)
      loadWorkbook(meta, sheets, f.path, 'excel')
      // sheet→PDF lineage from the workbook's embedded SheetSources property (or the Source
      // Ledger fallback) — sheet-sync works once the matching source PDFs are attached.
      await applySheetSources(buf, sheets)
      toast('success', 'Excel file uploaded successfully.')
    } catch (e) {
      console.error('[upload] excel parse/render failed', e)
      fail(MSG.generic)
    }
  }

  // ── extraction ────────────────────────────────────────────────────────────

  async function startExtraction() {
    if (!pdfs.length) return
    setJob(null)
    setMode('extracting')

    // pass the optional template path — backend uses it for template-driven mapping
    const res = await window.api.createExtractionJob(
      pdfs.map((f) => f.path),
      template?.path
    )
    if (res.status !== 200) {
      console.error('[upload] createExtractionJob failed', { status: res.status, body: res.body })
      return fail(MSG.generic)
    }

    const id = (res.body as { job_id: string }).job_id
    setJobId(id)

    // enriched polling — render progress.stage, progress.pct, progress.pdfs
    poll.current = setInterval(async () => {
      const r = await window.api.request({ method: 'GET', path: `/api/extraction/jobs/${id}` })
      if (r.status === 404) {
        // Job evicted from the server mid-flight
        stopPoll()
        toast('info', 'Extraction job is no longer available.')
        resetToChoose()
        return
      }
      if (r.status !== 200) return
      const j = r.body as Job
      setJob(j)
      if (j.status === 'done') {
        stopPoll()
        setCancelling(false)
        setMode('review')
      } else if (j.status === 'cancelled') {
        stopPoll()
        toast('info', 'Extraction cancelled successfully.')
        resetToChoose()
      } else if (j.status === 'failed') {
        stopPoll()
        const detail = j.progress?.detail ?? j.message ?? MSG.generic
        console.error('[upload] extraction job failed', detail)
        fail(detail)
      }
    }, 1500)
  }

  // ── review / ingest ───────────────────────────────────────────────────────

  async function review() {
    if (!jobId) return
    setMode('working')
    setBusyName('extracted workbook')
    setBusySize(0)
    const res = await window.api.ingestJobResult(jobId)
    if (res.status !== 200 || !res.path) {
      console.error('[upload] ingestJobResult failed', { status: res.status, body: res.body })
      return fail(MSG.generic)
    }
    try {
      const meta   = res.body as SessionMeta
      const buf    = await window.api.readFile(res.path)
      const sheets = await parseWorkbook(buf, meta.sheets)
      loadWorkbook(meta, sheets, res.path, 'ocr')
      setPdfPaths(pdfs.map((f) => f.path))
      setValidation(job ? pickValidation(job) : null)
      // prefer the workbook's embedded SheetSources; fall back to the job API result
      await applySheetSources(buf, sheets)
      if (!Object.keys(useApp.getState().sheetSources).length && job?.sheet_sources)
        setSheetSources(job.sheet_sources)
      setPanel('pdf', true)
      toast('success', `Extracted ${meta.company}`)
    } catch (e) {
      console.error('[upload] extracted workbook parse/render failed', e)
      fail(MSG.generic)
    }
  }

  function fail(msg: string) {
    stopPoll()
    setCancelling(false)
    toast('error', msg)
    setMode('choose')
  }

  function resetToChoose() {
    setPdfs([]); setTemplate(null); setJobId(null); setJob(null); setCancelling(false)
    setMode('choose')
  }

  // ── cancel flow ───────────────────────────────────────────────────────────

  async function confirmCancel() {
    if (!jobId) return
    setShowCancelConfirm(false)
    setCancelling(true) // disable button, show spinner — keep polling until terminal

    const r = await window.api.cancelExtractionJob(jobId)

    if (r.status === 404) {
      // Job already gone from the server — treat as cleanly cancelled
      stopPoll()
      toast('info', 'Extraction job is no longer available.')
      resetToChoose()
      return
    }

    if (r.status === 200) {
      const body = r.body as { job_id: string; status: string }
      // Race: job reached a terminal state before the cancel signal landed
      if (body.status === 'done') {
        // Job completed — go to review so the user doesn't lose the work
        setCancelling(false)
        // Re-fetch for full validation fields the DELETE response doesn't include
        const jr = await window.api.request({ method: 'GET', path: `/api/extraction/jobs/${jobId}` })
        if (jr.status === 200) setJob(jr.body as Job)
        stopPoll()
        setMode('review')
        return
      }
      if (TERMINAL.has(body.status as JobStatus)) {
        // failed or already cancelled — no error toast (idempotent)
        stopPoll()
        resetToChoose()
        return
      }
      // body.status === 'cancelling' — worker will stop at its next checkpoint;
      // existing poll loop will observe 'cancelled' and clean up.
    }
    // status 0 (network error) — leave cancelling=true, poll will eventually timeout
  }

  // ── choose-screen file picking ────────────────────────────────────────────

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

  // ── derived progress values (for extracting / review screens) ────────────

  const progress  = job?.progress ?? null
  const overallPct   = progress?.pct  ?? (mode === 'review' ? 100 : 0)
  const overallStage = progress?.stage ?? (mode === 'review' ? 'done' : 'queued')

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <>
      {/* ── backdrop ── */}
      <div
        className="fixed inset-0 z-40 flex items-center justify-center bg-black/50 backdrop-blur-sm"
        onClick={dismiss}
      >
        <div
          className="w-[600px] rounded-2xl bg-panel border border-line shadow-2xl p-6 flex flex-col"
          style={{ maxHeight: '88vh' }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* ── header ── */}
          <div className="flex items-center gap-2 mb-1 shrink-0">
            <UploadCloud className="w-5 h-5 text-accent" />
            <h2 className="text-base font-semibold">
              {mode === 'stage'      ? 'Prepare Extraction'   :
               mode === 'extracting' ? 'Extracting Documents' :
               mode === 'review'     ? 'Extraction Complete'  :
                                       'Upload Files'}
            </h2>
          </div>

          {/* ════════════════ CHOOSE ════════════════ */}
          {mode === 'choose' && (
            <>
              <p className="text-sm text-muted mb-4 shrink-0">
                Upload your company&apos;s annual report PDFs to extract the financials, or an
                existing Excel workbook to analyze.
              </p>
              <div
                onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
                onDragLeave={() => setDrag(false)}
                onDrop={onDrop}
                onClick={onPick}
                className={
                  'flex-1 min-h-[260px] cursor-pointer rounded-xl border-2 border-dashed ' +
                  'flex flex-col items-center justify-center text-center transition-colors ' +
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

          {/* ════════════════ WORKING (bar loader) ════════════════ */}
          {mode === 'working' && (
            <>
              <p className="text-sm text-muted mb-4 shrink-0">
                Analyzing the workbook — reading sheets, styles and formulas…
              </p>
              <SectionHeader label="Workbook" />
              <WorkingFileRow icon={excelIcon} name={busyName} size={busySize} />
            </>
          )}

          {/* ════════════════ STAGE ════════════════ */}
          {mode === 'stage' && (
            <>
              <p className="text-sm text-muted mb-4 shrink-0">
                Review the documents below, optionally attach an output template, then click{' '}
                <strong>Extract</strong> to begin.
              </p>

              {/* PDF list */}
              <SectionHeader label={`Source PDFs (${pdfs.length}/${PDF_MAX_COUNT})`}>
                {pdfs.length < PDF_MAX_COUNT && (
                  <InlineAction onClick={addMorePdfs}>
                    <Plus className="w-3 h-3" /> Add PDF
                  </InlineAction>
                )}
              </SectionHeader>
              <div className="space-y-2 max-h-52 overflow-auto mb-4">
                {pdfs.map((f) => (
                  <StagedFileRow
                    key={f.path}
                    icon={pdfIcon}
                    name={f.name}
                    size={f.size}
                    onRemove={() => removePdf(f.path)}
                  />
                ))}
              </div>

              {/* Template */}
              <SectionHeader label="Output Template (optional)">
                {!template && (
                  <InlineAction onClick={pickTemplate}>
                    <Plus className="w-3 h-3" /> Add template
                  </InlineAction>
                )}
              </SectionHeader>
              {template ? (
                <StagedFileRow
                  icon={excelIcon}
                  name={template.name}
                  size={template.size}
                  onRemove={() => setTemplate(null)}
                />
              ) : (
                <div
                  onClick={pickTemplate}
                  className={
                    'flex items-center justify-center gap-2 rounded-lg border border-dashed border-line ' +
                    'px-3 py-3 text-sm text-muted cursor-pointer hover:border-accent/50 ' +
                    'hover:text-accent/80 transition-colors'
                  }
                >
                  <img src={excelIcon} className="w-5 h-5 opacity-40" alt="" />
                  Click to attach an Excel template — enables template-driven extraction
                </div>
              )}

              {/* Actions */}
              <div className="mt-5 flex justify-between items-center shrink-0">
                <Button variant="ghost" onClick={back}>← Back</Button>
                <Button onClick={startExtraction} disabled={pdfs.length === 0}>
                  Extract
                </Button>
              </div>
            </>
          )}

          {/* ════════════════ EXTRACTING / REVIEW ════════════════ */}
          {(mode === 'extracting' || mode === 'review') && (
            <>
              <p className="text-sm text-muted mb-4 shrink-0">
                {mode === 'extracting'
                  ? 'Financial data is being extracted from the report(s)…'
                  : 'Extraction complete — review the summary and load the workbook.'}
              </p>

              {/* ── overall progress bar ── */}
              <div className="mb-4 shrink-0">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="text-xs font-medium text-muted">
                    {STAGE_LABEL[overallStage]}
                  </span>
                  <span className="text-xs text-muted tabular-nums">{overallPct}%</span>
                </div>
                <div className="h-1.5 rounded-full bg-panel2 overflow-hidden">
                  <div
                    className={
                      'h-full rounded-full transition-all duration-700 ' +
                      (mode === 'review'
                        ? 'bg-green-500'
                        : overallStage === 'failed' || overallStage === 'cancelled'
                          ? 'bg-red-500'
                          : cancelling
                            ? 'bg-muted'
                            : 'bg-accent')
                    }
                    style={{ width: `${overallPct}%` }}
                  />
                </div>
              </div>

              {/* ── per-PDF rows ── */}
              <SectionHeader label="Source PDFs" />
              <div className="space-y-2 max-h-48 overflow-auto mb-3">
                {pdfs.map((f) => {
                  // match by filename (progress.pdfs keys are filenames, not full paths)
                  const pdfStage: PipelineStage =
                    progress?.pdfs?.[f.name] ??
                    (mode === 'review' ? 'done' : 'queued')
                  return (
                    <ProgressFileRow
                      key={f.path}
                      icon={pdfIcon}
                      name={f.name}
                      size={f.size}
                      stage={pdfStage}
                    />
                  )
                })}
              </div>

              {/* ── template row (if provided) ── */}
              {template && (
                <>
                  <SectionHeader label="Template" />
                  <div className="mb-3">
                    <ProgressFileRow
                      icon={excelIcon}
                      name={template.name}
                      size={template.size}
                      stage={
                        mode === 'review' ? 'done' :
                        overallStage === 'mapping' ? 'mapping' : 'queued'
                      }
                    />
                  </div>
                </>
              )}

              {/* ── validation summary (review only) ── */}
              {mode === 'review' && job && <ValidationCard job={job} />}

              {/* ── actions ── */}
              <div className="mt-5 flex justify-between items-center shrink-0">
                {mode === 'extracting' ? (
                  <Button
                    variant="ghost"
                    disabled={cancelling}
                    onClick={() => !cancelling && setShowCancelConfirm(true)}
                    className="flex items-center gap-2"
                  >
                    {cancelling ? (
                      <>
                        <span className="h-3.5 w-3.5 rounded-full border-2 border-muted border-t-transparent animate-spin shrink-0" />
                        Cancelling…
                      </>
                    ) : 'Cancel'}
                  </Button>
                ) : (
                  <Button variant="ghost" onClick={back}>← Back</Button>
                )}
                {mode === 'review' && (
                  <Button onClick={review}>Load Workbook</Button>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* ════════════════ CANCEL CONFIRMATION ════════════════ */}
      {showCancelConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div
            className="w-[420px] rounded-2xl bg-panel border border-line shadow-2xl p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold mb-2">Cancel Extraction?</h3>
            <p className="text-sm text-muted mb-5">
              Extraction is still in progress. Cancelling now will stop the process and discard
              any partial results — you will need to start over to extract these documents.
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setShowCancelConfirm(false)}>
                Keep Extracting
              </Button>
              <Button variant="destructive" onClick={confirmCancel}>
                Yes, Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

// ── Section header ────────────────────────────────────────────────────────────

function SectionHeader({
  label,
  children
}: {
  label: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between mb-2 shrink-0">
      <span className="text-xs font-medium text-muted uppercase tracking-wide">{label}</span>
      {children}
    </div>
  )
}

function InlineAction({
  onClick,
  children
}: {
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1 text-xs text-accent hover:underline"
    >
      {children}
    </button>
  )
}

// ── Staged file row (stage screen — with remove button) ───────────────────────

function StagedFileRow({
  icon,
  name,
  size,
  onRemove
}: {
  icon: string
  name: string
  size: number
  onRemove: () => void
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-panel2 border border-line px-3 py-2">
      <img src={icon} className="w-7 h-7 shrink-0" alt="" />
      <div className="flex-1 min-w-0">
        <div className="truncate text-sm">{name}</div>
        <div className="text-xs text-muted mt-0.5">{kb(size)}</div>
      </div>
      <button
        onClick={onRemove}
        title="Remove"
        className="shrink-0 p-1.5 rounded text-muted hover:text-red-400 hover:bg-red-400/10 transition-colors"
      >
        <TrashIcon />
      </button>
    </div>
  )
}

// ── Progress file row (extracting / review — with stage chip) ─────────────────

function ProgressFileRow({
  icon,
  name,
  size,
  stage
}: {
  icon: string
  name: string
  size: number
  stage: PipelineStage
}) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-panel2 border border-line px-3 py-2">
      <img src={icon} className="w-7 h-7 shrink-0" alt="" />
      <div className="flex-1 min-w-0">
        <div className="truncate text-sm">{name}</div>
        <div className="text-xs text-muted mt-0.5">{kb(size)}</div>
      </div>
      <StageChip stage={stage} />
    </div>
  )
}

// ── Working file row (indeterminate bar — no measurable % yet) ────────────────

function WorkingFileRow({ icon, name, size }: { icon: string; name: string; size: number }) {
  return (
    <div className="flex items-center gap-3 rounded-lg bg-panel2 border border-line px-3 py-2.5">
      <img src={icon} className="w-7 h-7 shrink-0" alt="" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center justify-between gap-3">
          <div className="truncate text-sm">{name}</div>
          {size > 0 && (
            <span className="shrink-0 text-xs text-muted tabular-nums">{kb(size)}</span>
          )}
        </div>
        <div className="relative mt-1.5 h-1.5 rounded-full bg-line/60 overflow-hidden">
          <span className="indeterminate-bar bg-accent" />
        </div>
      </div>
    </div>
  )
}

// ── Stage chip ────────────────────────────────────────────────────────────────

function StageChip({ stage }: { stage: PipelineStage }) {
  const base = 'shrink-0 inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium'
  if (stageIsDone(stage))
    return <span className={`${base} bg-green-500/15 text-green-400`}>{STAGE_LABEL[stage]}</span>
  if (stageIsFailed(stage))
    return <span className={`${base} bg-red-500/15 text-red-400`}>Failed</span>
  if (stageIsCancelled(stage))
    return <span className={`${base} bg-muted/15 text-muted line-through`}>Cancelled</span>
  if (stageIsActive(stage))
    return (
      <span className={`${base} bg-accent/15 text-accent`}>
        <span className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full bg-accent animate-pulse" />
        {STAGE_LABEL[stage]}
      </span>
    )
  // queued
  return <span className={`${base} bg-muted/15 text-muted`}>Queued</span>
}

// ── Validation card ───────────────────────────────────────────────────────────

function ValidationCard({ job }: { job: Job }) {
  const items: [string, unknown][] = [
    ['Production ready',   job.production_ready ? 'Yes' : 'No'],
    ['Validation failures', job.validation_failures ?? 0],
    ['Withheld',           job.withheld ?? 0],
    ['Quarantined',        job.quarantined ?? 0]
  ]
  return (
    <div className="mt-3 rounded-lg border border-line bg-panel2 px-3 py-2.5 shrink-0">
      <div className="text-xs font-medium text-muted mb-1.5">Extraction Quality</div>
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

// ── Trash icon ────────────────────────────────────────────────────────────────

function TrashIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width="15" height="15"
      viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round"
    >
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </svg>
  )
}

// ── pickValidation ────────────────────────────────────────────────────────────

function pickValidation(j: Job): ValidationSummary {
  return {
    production_ready:    j.production_ready,
    fully_reconciled:    j.fully_reconciled,
    validation_failures: j.validation_failures,
    detail_incomplete:   j.detail_incomplete,
    withheld:            j.withheld,
    quarantined:         j.quarantined
  }
}
