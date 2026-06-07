import { useState, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import { useApp } from '@/store'
import type { Citation, FieResponse } from '@/api'
import { cn } from '@/lib/util'

const SUGGESTIONS = [
  'What was revenue in 2024?',
  'Current ratio for 2024',
  'Key risks',
  'Revenue trend over the years'
]

export function AskAI() {
  const { chat, ask, session } = useApp()
  const [input, setInput] = useState('')

  function send(q?: string) {
    const text = (q ?? input).trim()
    if (!text || chat.pending) return
    setInput('')
    ask(text)
  }
  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="h-full flex flex-col bg-panel">
      <div className="h-9 shrink-0 flex items-center px-3 border-b border-line text-sm font-medium">
        Ask AI
        <span className="ml-2 text-xs text-muted truncate">· {session?.company}</span>
      </div>

      <div className="flex-1 min-h-0 overflow-auto p-3 space-y-3">
        {chat.messages.length === 0 && (
          <div className="text-sm text-muted">
            Ask about this workbook. Every answer cites its source.
            <div className="mt-3 flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="text-xs rounded-full border border-line bg-panel2 px-2.5 py-1 hover:border-accent/60"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {chat.messages.map((m) =>
          m.role === 'user' ? (
            <div key={m.id} className="flex justify-end">
              <div className="max-w-[85%] rounded-lg bg-accent/15 border border-accent/30 px-3 py-2 text-sm">
                {m.text}
              </div>
            </div>
          ) : (
            <div key={m.id} className="max-w-[92%]">
              {m.response ? (
                <AnswerCard r={m.response} />
              ) : m.error ? (
                <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                  {m.error}
                </div>
              ) : (
                <div className="flex items-center gap-2 text-sm text-muted">
                  <span className="h-3 w-3 rounded-full border-2 border-muted border-t-transparent animate-spin" />
                  thinking…
                </div>
              )}
            </div>
          )
        )}
      </div>

      <div className="shrink-0 border-t border-line p-2.5">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          rows={2}
          placeholder="Ask a question…"
          className="w-full resize-none rounded-lg bg-panel2 border border-line px-3 py-2 text-sm focus:outline-none focus:border-accent/60"
        />
      </div>
    </div>
  )
}

function AnswerCard({ r }: { r: FieResponse }) {
  const cov = r.coverage || {}
  const insufficient = !!(cov as { insufficient_evidence?: boolean }).insufficient_evidence
  const degraded = !!(cov as { degraded?: boolean }).degraded
  return (
    <div className="rounded-lg border border-line bg-panel2 p-3 space-y-2.5 text-sm">
      <div className="flex items-start gap-2">
        <ConfidenceBadge band={r.confidence?.band} />
        <div className="flex-1">{r.direct_answer}</div>
      </div>

      {r.key_findings.length > 0 && (
        <ul className="space-y-1.5 pl-1">
          {r.key_findings.map((f, i) => (
            <li key={i} className="flex gap-2">
              <span className="text-muted">•</span>
              <span>{renderWithCites(f, r.citations)}</span>
            </li>
          ))}
        </ul>
      )}

      {(insufficient || degraded) && (
        <div className="flex flex-wrap gap-1.5">
          {insufficient && <Caveat text="insufficient citable evidence" />}
          {degraded && <Caveat text="degraded — some sources unavailable" />}
        </div>
      )}

      {r.conflicts?.length > 0 && (
        <div className="space-y-1">
          {r.conflicts.map((c, i) => (
            <div key={i} className="text-xs text-amber-300">
              Divergence on {c.topic}: {c.resolution || (c.resolved ? 'resolved' : 'surfaced')}
            </div>
          ))}
        </div>
      )}

      {r.supporting_analysis && (
        <div className="prose-invert text-[13px] text-muted leading-snug border-t border-line pt-2">
          <ReactMarkdown>{r.supporting_analysis}</ReactMarkdown>
        </div>
      )}

      {r.citations.length > 0 && (
        <div className="border-t border-line pt-2 space-y-0.5">
          {r.citations.map((c) => (
            <div key={c.ref_id} className="text-[11px] text-muted">
              <span className="text-accent">[{c.ref_id}]</span> {c.display}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function renderWithCites(text: string, cites: Citation[]) {
  return text.split(/(\[C\d+\])/g).map((part, i) => {
    const m = part.match(/^\[(C\d+)\]$/)
    if (!m) return <span key={i}>{part}</span>
    const c = cites.find((x) => x.ref_id === m[1])
    return <CitationChip key={i} refId={m[1]} cite={c} />
  })
}

function CitationChip({ refId, cite }: { refId: string; cite?: Citation }) {
  const { onCitation, toast } = useApp()
  return (
    <button
      title={cite?.display ?? refId}
      onClick={() => (cite ? onCitation(cite) : toast('info', refId))}
      className="mx-0.5 align-baseline rounded bg-accent/15 text-accent border border-accent/30 px-1 text-[11px] hover:bg-accent/25"
    >
      {refId}
    </button>
  )
}

function ConfidenceBadge({ band }: { band?: 'High' | 'Medium' | 'Low' }) {
  if (!band) return null
  const cls =
    band === 'High'
      ? 'text-green-400 border-green-500/40 bg-green-500/10'
      : band === 'Medium'
        ? 'text-amber-300 border-amber-500/40 bg-amber-500/10'
        : 'text-red-300 border-red-500/40 bg-red-500/10'
  return (
    <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[11px] border', cls)}>{band}</span>
  )
}

function Caveat({ text }: { text: string }) {
  return (
    <span className="rounded px-1.5 py-0.5 text-[11px] border border-amber-500/40 bg-amber-500/10 text-amber-300">
      {text}
    </span>
  )
}
