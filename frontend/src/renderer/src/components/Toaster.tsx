import { useEffect, useRef } from 'react'
import { useApp, type Toast } from '@/store'

const TOAST_MS = 10_000 // stays 10s unless hovered (matches the attached project)

// Light-mode palette per type (Untitled-UI values used by the reference toast service)
const STYLE: Record<Toast['kind'], { bg: string; border: string; text: string; stroke: string }> = {
  success: { bg: '#ECFDF3', border: '#ABEFC6', text: '#067647', stroke: '#079455' },
  error: { bg: '#FEF3F2', border: '#FECDCA', text: '#B42318', stroke: '#D92D20' },
  warning: { bg: '#FFFAEB', border: '#FEDF89', text: '#B54708', stroke: '#DC6803' },
  info: { bg: '#EFF8FF', border: '#B2DDFF', text: '#026AA2', stroke: '#0086C9' }
}

/** Transient notifications, top-right (frontend-spec §11). */
export function Toaster() {
  const { toasts, dismissToast } = useApp()
  return (
    <div className="fixed top-3 right-3 z-50 flex flex-col items-end gap-2 max-w-[90vw]">
      {toasts.map((t) => (
        <ToastRow key={t.id} toast={t} onClose={() => dismissToast(t.id)} />
      ))}
    </div>
  )
}

function ToastRow({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const s = STYLE[toast.kind]

  const start = () => {
    timer.current = setTimeout(onClose, TOAST_MS)
  }
  const stop = () => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
  }
  useEffect(() => {
    start()
    return stop
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div
      onMouseEnter={stop}
      onMouseLeave={start}
      className="flex items-center justify-between gap-3 rounded-md border-2 px-3.5 py-2.5 shadow-lg w-auto"
      style={{ background: s.bg, borderColor: s.border, color: s.text }}
    >
      <div className="flex items-center gap-2">
        <TypeIcon kind={toast.kind} stroke={s.stroke} />
        <span className="text-sm font-medium whitespace-nowrap">{toast.text}</span>
      </div>
      <button onClick={onClose} title="Close" className="shrink-0 mt-0.5">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path
            d="M3 13.5L13 3.5M13 13.5L3 3.5"
            stroke={s.stroke}
            strokeWidth="1.66667"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
    </div>
  )
}

function TypeIcon({ kind, stroke }: { kind: Toast['kind']; stroke: string }) {
  if (kind === 'success') {
    return (
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" className="shrink-0" xmlns="http://www.w3.org/2000/svg">
        <path
          d="M6.25 10L8.75 12.5L13.75 7.5M18.3333 10C18.3333 14.6024 14.6024 18.3333 10 18.3333C5.39763 18.3333 1.66667 14.6024 1.66667 10C1.66667 5.39763 5.39763 1.66667 10 1.66667C14.6024 1.66667 18.3333 5.39763 18.3333 10Z"
          stroke={stroke}
          strokeWidth="1.66667"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    )
  }
  // error / warning / info share the alert/info-circle glyph
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" className="shrink-0" xmlns="http://www.w3.org/2000/svg">
      <path
        d="M10 13.3333V10M10 6.66667H10.0083M18.3333 10C18.3333 14.6024 14.6024 18.3333 10 18.3333C5.39763 18.3333 1.66667 14.6024 1.66667 10C1.66667 5.39763 5.39763 1.66667 10 1.66667C14.6024 1.66667 18.3333 5.39763 18.3333 10Z"
        stroke={stroke}
        strokeWidth="1.66667"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}
