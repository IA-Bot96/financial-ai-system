import { useCallback, useEffect, useState } from 'react'
import { useApp } from '@/store'
import { api } from '@/api'
import { Splash } from '@/components/Splash'
import { ErrorScreen } from '@/components/ErrorScreen'
import { LeftRail } from '@/components/LeftRail'
import { RightRail } from '@/components/RightRail'
import { Toaster } from '@/components/Toaster'
import { UploadModal } from '@/components/UploadModal'
import { AttachPdfModal } from '@/components/AttachPdfModal'
import { ConfirmDiscardModal } from '@/components/ConfirmDiscardModal'
import { SaveBar } from '@/components/SaveBar'
import { SheetToolbar } from '@/components/SheetToolbar'
import { SheetView } from '@/components/SheetView'
import { AskAI } from '@/components/AskAI'
import { PdfPanel } from '@/components/PdfPanel'
import { PanelResizer } from '@/components/PanelResizer'
import { Dashboard } from '@/components/Dashboard'

const POLL_MS = 400
const TIMEOUT_MS = 40_000

export default function App() {
  const { backend, setBackend, session, view, panels, panelWidth, uploadOpen, attachPdfsOpen } =
    useApp()
  const [msg, setMsg] = useState('Starting the analysis engine…')

  const boot = useCallback(async () => {
    setBackend('starting')
    setMsg('Starting the analysis engine…')
    const logPath = await window.api.getBackendLogPath().catch(() => '')
    const deadline = Date.now() + TIMEOUT_MS
    // Gate on /health (backend reachable). /readiness needs a delivered workbook, which
    // the desktop doesn't require — the user opens one via the app.
    while (Date.now() < deadline) {
      const r = await api.health().catch(() => ({ status: 0, body: null }))
      if (r.status === 200) {
        setBackend('ready')
        return
      }
      await new Promise((res) => setTimeout(res, POLL_MS))
    }
    setBackend('error', logPath)
  }, [setBackend])

  useEffect(() => {
    boot()
  }, [boot])

  // native menu + Ctrl+S/O shortcuts → store actions
  useEffect(() => {
    const s = useApp.getState
    window.api.onMenu((action) => {
      if (action === 'open') s().openUpload()
      else if (action === 'save') s().save()
      else if (action === 'saveAs') s().save(true)
      else if (action === 'togglePdf') s().togglePanel('pdf')
      else if (action === 'toggleAskAI') s().togglePanel('askAI')
      else if (action === 'dashboard') s().setView('dashboard')
    })
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        useApp.getState().save(e.shiftKey)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  if (backend.status === 'starting') return <Splash message={msg} />
  if (backend.status === 'error')
    return <ErrorScreen logPath={backend.logPath} onRetry={boot} />

  return (
    <div className="h-full w-full flex flex-col">
      <SaveBar />
      <div className="flex-1 flex min-h-0">
        <LeftRail />

        {/* PDF dock — LEFT, resizable. Only on the sheet surface (toggle state persists). */}
        {panels.pdf && session && view === 'sheet' && (
          <>
            <aside style={{ width: panelWidth.pdf }} className="shrink-0 border-r border-line">
              <PdfPanel />
            </aside>
            <PanelResizer panel="pdf" />
          </>
        )}

        {/* center primary surface. First load (no session) = an empty sheet behind the
            (forced-open, non-dismissable) upload modal. */}
        <main className="flex-1 min-w-0 flex flex-col">
          {!session ? (
            <SheetView />
          ) : view === 'dashboard' ? (
            <Dashboard />
          ) : (
            <>
              <SheetToolbar />
              <div className="flex-1 min-h-0">
                <SheetView />
              </div>
            </>
          )}
        </main>

        {/* Ask AI dock — RIGHT, resizable. Only on the sheet surface (toggle state persists). */}
        {panels.askAI && session && view === 'sheet' && (
          <>
            <PanelResizer panel="askAI" />
            <aside style={{ width: panelWidth.askAI }} className="shrink-0 border-l border-line">
              <AskAI />
            </aside>
          </>
        )}

        {/* far-right rail: PDF / Ask AI panel toggles */}
        <RightRail />
      </div>

      {(!session || uploadOpen) && <UploadModal />}
      {attachPdfsOpen && <AttachPdfModal />}
      <ConfirmDiscardModal />
      <Toaster />
      <div className="h-6 shrink-0 border-t border-line bg-panel text-[11px] text-muted px-3 flex items-center gap-3">
        <span>backend: ready</span>
        {session && <span>· session {session.session_id}</span>}
      </div>
    </div>
  )
}
