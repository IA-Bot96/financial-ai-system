import { presentIssue, type ValidationIssue, type ColorKey } from '@/lib/validation'

const DOT: Record<ColorKey, string> = {
  error: 'bg-red-500',
  warning: 'bg-amber-500',
  minor: 'bg-slate-400',
  verified: 'bg-green-500'
}

/**
 * Validation detail card. Used two ways:
 *  - hover (interactive=false): follows the cursor, read-only, pointer-events off.
 *  - click (interactive=true): pinned, the "Manually Verified" checkbox is live.
 * All numbers/text come from `presentIssue` (backend Validation Ledger) — never recomputed.
 */
export function ValidationCard({
  issue,
  x,
  y,
  interactive,
  mvAvailable,
  onVerify
}: {
  issue: ValidationIssue
  x: number
  y: number
  interactive: boolean
  mvAvailable: boolean
  onVerify: (checked: boolean) => void
}) {
  const p = presentIssue(issue)
  const showShownFound = !!(p.got && p.found)
  const meta = [issue.metric && `Metric: ${issue.metric}`, issue.year && `Year: ${issue.year}`]
    .filter(Boolean)
    .join('  ·  ')

  return (
    <div
      data-validation-card
      className={
        'absolute z-50 w-[330px] rounded-lg border border-line bg-panel px-3 py-2.5 ' +
        'shadow-xl shadow-black/50 text-xs ' +
        (interactive ? '' : 'pointer-events-none')
      }
      style={{ left: x, top: y }}
    >
      {/* header — title on its own line; the cell/sheet caption sits beneath it so a long
          title never gets squeezed/wrapped by the location text */}
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full shrink-0 ${DOT[p.color]}`} />
        <span className="font-semibold text-ink leading-snug">{p.title}</span>
      </div>
      <div className="text-[11px] text-muted mt-0.5 pl-4">
        {issue.cell ?? issue.cellLabel}
        {issue.sheet ? ` · ${issue.sheet}` : ''}
      </div>

      <div className="text-ink/90 leading-snug mt-1.5">{p.message}</div>

      {showShownFound && (
        <div className="text-ink/80 tabular-nums mt-1.5">
          Shown {p.got}, we found {p.found}
          {p.page}
        </div>
      )}
      {meta && <div className="text-muted mt-1">{meta}</div>}

      {/* Manually Verified */}
      <div className="mt-2 pt-2 border-t border-line">
        <label
          className={
            'flex items-center gap-2 ' +
            (interactive && mvAvailable ? 'cursor-pointer' : 'opacity-60 cursor-default')
          }
          title={mvAvailable ? undefined : 'Regenerate the workbook to enable verification'}
        >
          <input
            type="checkbox"
            checked={issue.verified}
            disabled={!interactive || !mvAvailable}
            onChange={(e) => onVerify(e.target.checked)}
            className="h-3.5 w-3.5 accent-green-500 rounded"
          />
          <span className="text-ink">Manually Verified</span>
        </label>
      </div>
    </div>
  )
}
