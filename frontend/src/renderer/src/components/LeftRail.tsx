import { useApp } from '@/store'
import { cn } from '@/lib/util'

type Item = { key: string; label: string; needsSession?: boolean }

const ITEMS: Item[] = [
  { key: 'home', label: 'Home' },
  { key: 'sheet', label: 'Sheet', needsSession: true },
  { key: 'pdf', label: 'PDF', needsSession: true },
  { key: 'askAI', label: 'Ask AI', needsSession: true },
  { key: 'dashboard', label: 'Dashboard', needsSession: true }
]

export function LeftRail() {
  const { session, view, panels, setView, togglePanel } = useApp()
  const hasSession = !!session

  function onClick(key: string) {
    if (key === 'home') return setView('home')
    if (key === 'sheet') return setView('sheet')
    if (key === 'dashboard') return setView('dashboard')
    if (key === 'pdf') return togglePanel('pdf')
    if (key === 'askAI') return togglePanel('askAI')
  }

  function active(key: string): boolean {
    if (key === 'pdf') return panels.pdf
    if (key === 'askAI') return panels.askAI
    return view === key
  }

  return (
    <nav className="w-[76px] shrink-0 h-full bg-panel border-r border-line flex flex-col items-center py-3 gap-1">
      <div className="h-9 w-9 rounded-lg bg-accent/15 border border-accent/40 flex items-center justify-center text-accent text-sm font-bold mb-2">
        FI
      </div>
      {ITEMS.map((it) => {
        const disabled = it.needsSession && !hasSession
        return (
          <button
            key={it.key}
            disabled={disabled}
            onClick={() => onClick(it.key)}
            className={cn(
              'w-[60px] py-2 rounded-lg text-[11px] font-medium flex flex-col items-center gap-1',
              'text-muted hover:bg-panel2 hover:text-ink transition-colors',
              active(it.key) && 'bg-panel2 text-accent',
              disabled && 'opacity-30 cursor-not-allowed hover:bg-transparent'
            )}
          >
            <span className="h-5 w-5 rounded-md border border-current/40" />
            {it.label}
          </button>
        )
      })}
    </nav>
  )
}
