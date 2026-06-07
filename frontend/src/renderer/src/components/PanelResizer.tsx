import { MouseEvent } from 'react'
import { useApp } from '@/store'

const RAIL = 76 // left-rail width

/** Thin draggable divider on a dock's inner edge; updates the panel width in the store. */
export function PanelResizer({ panel }: { panel: 'pdf' | 'askAI' }) {
  const setPanelWidth = useApp((s) => s.setPanelWidth)
  function onDown(e: MouseEvent) {
    e.preventDefault()
    const move = (ev: globalThis.MouseEvent) => {
      const w = panel === 'askAI' ? window.innerWidth - ev.clientX : ev.clientX - RAIL
      setPanelWidth(panel, w)
    }
    const up = () => {
      document.removeEventListener('mousemove', move)
      document.removeEventListener('mouseup', up)
    }
    document.addEventListener('mousemove', move)
    document.addEventListener('mouseup', up)
  }
  return (
    <div
      onMouseDown={onDown}
      className="w-1.5 shrink-0 cursor-col-resize bg-line hover:bg-accent/60 transition-colors"
    />
  )
}
