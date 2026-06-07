import { ComponentType } from 'react'
import { useApp } from '@/store'
import { cn } from '@/lib/util'
import { File, Stars } from './ui/icons'

type PanelKey = 'pdf' | 'askAI'
const ITEMS: { key: PanelKey; label: string; Icon: ComponentType<{ className?: string }> }[] = [
  { key: 'pdf', label: 'PDF', Icon: File },
  { key: 'askAI', label: 'Ask AI', Icon: Stars }
]

/**
 * Narrow right rail holding the auxiliary-panel toggles (PDF dock + Ask AI dock). Kept
 * separate from the left navigation rail so toggle (on/off) controls don't read like
 * navigation destinations. Only shown once a workbook is open.
 */
export function RightRail() {
  const session = useApp((s) => s.session)
  const view = useApp((s) => s.view)
  const panels = useApp((s) => s.panels)
  const pdfPaths = useApp((s) => s.pdfPaths)
  const togglePanel = useApp((s) => s.togglePanel)
  // the PDF / Ask AI docks only apply to the sheet surface — hide the rail elsewhere
  if (!session || view !== 'sheet') return null

  return (
    <nav className="w-[60px] shrink-0 h-full bg-panel2 border-l border-line flex flex-col items-center py-3 gap-1">
      {ITEMS.map(({ key, label, Icon }) => {
        // no source PDF for this workbook (e.g. an Excel upload) -> PDF toggle is disabled
        const disabled = key === 'pdf' && pdfPaths.length === 0
        return (
          <button
            key={key}
            onClick={() => togglePanel(key)}
            disabled={disabled}
            aria-pressed={panels[key]}
            title={disabled ? 'No source PDF for this workbook' : undefined}
            className={cn(
              'w-[52px] py-2 rounded-lg text-[11px] font-medium flex flex-col items-center gap-1',
              'text-muted hover:bg-line hover:text-ink transition-colors whitespace-nowrap',
              panels[key] && 'bg-accent/20 text-accent',
              disabled && 'opacity-30 cursor-not-allowed hover:bg-transparent hover:text-muted'
            )}
          >
            <Icon className="h-5 w-5" />
            {label}
          </button>
        )
      })}
    </nav>
  )
}
