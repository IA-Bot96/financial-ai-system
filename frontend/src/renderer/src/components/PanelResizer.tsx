import { MouseEvent } from 'react'
import { useApp } from '@/store'

const LEFT_RAIL = 76
const RIGHT_RAIL = 60

/** Thin draggable divider on a dock's inner edge; updates the panel width in the store. */
export function PanelResizer({ panel }: { panel: 'pdf' | 'askAI' | 'history' }) {
  const setPanelWidth = useApp((s) => s.setPanelWidth)
  function onDown(e: MouseEvent) {
    e.preventDefault()
    const move = (ev: globalThis.MouseEvent) => {
      // askAI + history dock on the RIGHT (width measured from the right edge); pdf on the LEFT.
      const w = panel === 'pdf' ? ev.clientX - LEFT_RAIL : window.innerWidth - ev.clientX - RIGHT_RAIL
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
