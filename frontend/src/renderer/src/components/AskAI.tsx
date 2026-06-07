import { useEffect, useRef, useState, KeyboardEvent } from 'react'
import ReactMarkdown from 'react-markdown'
import { useApp } from '@/store'
import type { Citation, FieResponse } from '@/api'
import { cn } from '@/lib/util'

const AI_NAME = 'Ask AI'
const USER_NAME = 'You'

const SUGGESTIONS = [
  'What was revenue in 2024?',
  'Current ratio for 2024',
  'Key risks',
  'Revenue trend over the years'
]

// ---- timestamp helpers -------------------------------------------------------
function relativeTime(ts: number, now: number): string {
  const diff = Math.floor((now - ts) / 1000)
  if (diff < 10) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function useNow() {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 15_000)
    return () => clearInterval(id)
  }, [])
  return now
}

// ---- icons -------------------------------------------------------------------
function AiAvatar() {
  return (
    <div className="h-7 w-7 rounded-full shrink-0 flex items-center justify-center bg-[#1e1229]">
      <svg viewBox="0 0 20 20" fill="none" className="h-4 w-4" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="aig" x1="0" y1="0" x2="20" y2="20" gradientUnits="userSpaceOnUse">
            <stop stopColor="#a855f7" />
            <stop offset="0.55" stopColor="#db2777" />
            <stop offset="1" stopColor="#f97316" />
          </linearGradient>
        </defs>
        <path
          d="M8.846 14.829l.732-1.676c.65-1.49 1.822-2.677 3.283-3.325l2.013-.894c.64-.284.64-1.215 0-1.499l-1.95-.865C11.425 5.904 10.233 4.673 9.593 3.132L8.852 1.346c-.275-.662-1.19-.662-1.465 0L6.646 3.132C6.006 4.673 4.814 5.904 3.315 6.569L1.365 7.435c-.64.284-.64 1.215 0 1.499l2.013.894c1.46.648 2.633 1.835 3.283 3.325l.731 1.676c.281.643 1.172.643 1.454 0zM16.169 18.907l.206-.472c.367-.84 1.027-1.51 1.851-1.876l.634-.281c.343-.153.343-.65 0-.803l-.599-.266c-.845-.375-1.517-1.07-1.877-1.939l-.211-.509c-.148-.355-.638-.355-.786 0l-.21.509c-.36.869-1.033 1.564-1.878 1.939l-.598.266c-.343.153-.343.65 0 .803l.633.281c.825.366 1.485 1.036 1.852 1.876l.205.472c.151.345.629.345.78 0z"
          fill="url(#aig)"
        />
      </svg>
    </div>
  )
}

function UserAvatar() {
  return (
    <div className="h-7 w-7 rounded-full shrink-0 flex items-center justify-center bg-accent/15 border border-accent/30">
      <svg viewBox="0 0 24 24" fill="none" className="h-4 w-4 text-accent" xmlns="http://www.w3.org/2000/svg">
        <path
          d="M12 15C8.83 15 6.01 16.531 4.216 18.906c-.387.511-.58.767-.574 1.112.005.267.173.604.383.769C4.297 21 4.674 21 5.427 21h13.146c.754 0 1.13 0 1.402-.213.21-.165.378-.502.382-.769.006-.345-.187-.601-.574-1.112C17.99 16.531 15.17 15 12 15zM12 12c2.485 0 4.5-2.015 4.5-4.5S14.485 3 12 3 7.5 5.015 7.5 7.5 9.515 12 12 12z"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </div>
  )
}

function MicIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M20 12v1c0 4.418-3.582 8-8 8s-8-3.582-8-8v-1M12 17c-2.209 0-4-1.791-4-4V7c0-2.209 1.791-4 4-4s4 1.791 4 4v6c0 2.209-1.791 4-4 4z"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

function SendIcon({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} xmlns="http://www.w3.org/2000/svg">
      <path
        d="M12 19V5M12 5L5 12M12 5L19 12"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// ---- main component ----------------------------------------------------------
export function AskAI() {
  const { chat, ask, session } = useApp()
  const [input, setInput] = useState('')
  const now = useNow()
  const bodyRef = useRef<HTMLDivElement>(null)
  const hasMessages = chat.messages.length > 0

  // auto-scroll to bottom on new messages
  useEffect(() => {
    const el = bodyRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [chat.messages])

  function send(q?: string) {
    const text = (q ?? input).trim()
    if (!text || chat.pending) return
    setInput('')
    ask(text)
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="h-full flex flex-col bg-panel">
      {/* header */}
      <div className="h-9 shrink-0 flex items-center gap-2 px-3 border-b border-line">
        <AiAvatar />
        <span className="text-sm font-medium">{AI_NAME}</span>
        {session?.company && (
          <span className="text-xs text-muted truncate">· {session.company}</span>
        )}
      </div>

      {/* chat body */}
      <div ref={bodyRef} className="flex-1 min-h-0 overflow-y-auto px-3 py-3 space-y-5">
        {/* initial greeting — hidden once the user sends anything */}
        {!hasMessages && (
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <AiAvatar />
              <span className="text-xs font-semibold text-ink">{AI_NAME}</span>
              <span className="text-[11px] text-muted">· just now</span>
            </div>
            <div className="ml-9 rounded-xl border border-line bg-panel2 px-4 py-3 text-sm space-y-3">
              <p className="text-muted leading-relaxed">
                Hi! I'm ready to help you analyze this workbook. Every answer cites its source
                so you can trace back to the exact cell or document.
              </p>
              <div className="flex flex-wrap gap-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="text-xs rounded-full border border-line bg-panel px-2.5 py-1 text-muted hover:border-accent/50 hover:text-ink transition-colors"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* conversation messages */}
        {chat.messages.map((m) =>
          m.role === 'user' ? (
            /* user bubble — right-aligned */
            <div key={m.id} className="flex flex-col items-end gap-1.5">
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-muted">{relativeTime(m.timestamp, now)}</span>
                <span className="text-xs font-semibold text-ink">{USER_NAME}</span>
                <UserAvatar />
              </div>
              <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-accent/15 border border-accent/25 px-3.5 py-2.5 text-sm leading-relaxed">
                {m.text}
              </div>
            </div>
          ) : (
            /* AI bubble — left-aligned */
            <div key={m.id} className="flex flex-col items-start gap-1.5">
              <div className="flex items-center gap-2">
                <AiAvatar />
                <span className="text-xs font-semibold text-ink">{AI_NAME}</span>
                <span className="text-[11px] text-muted">· {relativeTime(m.timestamp, now)}</span>
              </div>
              <div className="ml-9 max-w-full">
                {m.response ? (
                  <AnswerCard r={m.response} />
                ) : m.error ? (
                  <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-3.5 py-2.5 text-sm text-red-300">
                    {m.error}
                  </div>
                ) : (
                  <div className="flex items-center gap-2 text-sm text-muted px-1 py-1.5">
                    <span className="h-3.5 w-3.5 rounded-full border-2 border-muted border-t-transparent animate-spin" />
                    Thinking…
                  </div>
                )}
              </div>
            </div>
          )
        )}
      </div>

      {/* footer */}
      <div className="shrink-0 border-t border-line">
        {/* input card */}
        <div className="px-3 pt-2.5">
          <div className="rounded-xl border border-line bg-panel2 flex flex-col">
            {/* textarea */}
            <div className="relative px-3.5 pt-3 pb-1">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKey}
                rows={2}
                placeholder="Ask a question…"
                className="w-full resize-none bg-transparent text-sm text-ink placeholder:text-muted focus:outline-none leading-relaxed"
              />
            </div>
            {/* action row */}
            <div className="flex items-center justify-end gap-2 px-3 pb-2.5">
              {/* mic — disabled, decorative */}
              <button
                disabled
                title="Voice input not available"
                className="h-8 w-8 rounded-full flex items-center justify-center border border-line text-muted opacity-40 cursor-not-allowed"
              >
                <MicIcon className="h-4 w-4" />
              </button>
              {/* send */}
              <button
                onClick={() => send()}
                disabled={!input.trim() || chat.pending}
                title="Send (Enter)"
                className={cn(
                  'h-8 w-8 rounded-full flex items-center justify-center transition-colors',
                  input.trim() && !chat.pending
                    ? 'bg-accent text-white hover:bg-accent/80'
                    : 'bg-line text-muted opacity-40 cursor-not-allowed'
                )}
              >
                {chat.pending ? (
                  <span className="h-3.5 w-3.5 rounded-full border-2 border-current border-t-transparent animate-spin" />
                ) : (
                  <SendIcon className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>
        </div>

        {/* info line — below the input */}
        <div className="px-3 pt-1.5 pb-3 text-[11px] text-muted flex items-center justify-center gap-1.5">
          <svg viewBox="0 0 16 16" fill="none" className="h-3 w-3 shrink-0">
            <circle cx="8" cy="8" r="7" stroke="currentColor" strokeWidth="1.5" />
            <path d="M8 7v4M8 5.5V5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
          Ask about this workbook. Every answer cites its source.
        </div>
      </div>
    </div>
  )
}

// ---- answer card -------------------------------------------------------------
function AnswerCard({ r }: { r: FieResponse }) {
  const cov = r.coverage || {}
  const insufficient = !!(cov as { insufficient_evidence?: boolean }).insufficient_evidence
  const degraded = !!(cov as { degraded?: boolean }).degraded
  return (
    <div className="rounded-xl border border-line bg-panel2 px-3.5 py-3 space-y-2.5 text-sm">
      <div className="flex items-start gap-2">
        <ConfidenceBadge band={r.confidence?.band} />
        <div className="flex-1 leading-relaxed">{r.direct_answer}</div>
      </div>

      {r.key_findings.length > 0 && (
        <ul className="space-y-1.5 pl-1">
          {r.key_findings.map((f, i) => (
            <li key={i} className="flex gap-2">
              <span className="text-muted shrink-0">•</span>
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
