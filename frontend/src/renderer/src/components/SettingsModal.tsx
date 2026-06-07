import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type SettingsField } from '@/api'
import { useApp } from '@/store'
import { cn } from '@/lib/util'
import { Button } from './ui/Button'
import { Settings as Gear, ChevronDown } from './ui/icons'

// ── helpers ──────────────────────────────────────────────────────────────────

/** Ordered, de-duplicated list of group names as they first appear in `fields`. */
function groupOrder(fields: SettingsField[]): string[] {
  const seen: string[] = []
  for (const f of fields) if (!seen.includes(f.group)) seen.push(f.group)
  return seen
}

/** Non-blocking memory/perf warning for the knobs that cause OOM ingest crashes. */
function usageWarning(f: SettingsField, value: unknown): string | null {
  const n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return null
  if (f.key.includes('workers') && f.maximum != null) {
    if (n >= Math.max(2, Math.ceil(f.maximum * 0.75)))
      return 'High worker counts use more memory in parallel — can crash ingest on large PDFs.'
  }
  if (/dpi/i.test(f.key)) {
    const ceil = f.maximum ?? (Array.isArray(f.options) ? Math.max(...f.options.map(Number)) : null)
    if (ceil != null && n >= ceil * (f.kind === 'enum' ? 1 : 0.85))
      return 'Higher resolution uses more memory and is slower — 200 DPI is a safe default.'
  }
  return null
}

/** Render a default value as a short hint string. */
function fmtDefault(v: unknown): string {
  if (typeof v === 'boolean') return v ? 'On' : 'Off'
  if (v === '' || v == null) return '—'
  return String(v)
}

// ── component ────────────────────────────────────────────────────────────────

export function SettingsModal() {
  const closeSettings = useApp((s) => s.closeSettings)
  const toast = useApp((s) => s.toast)

  const [fields, setFields] = useState<SettingsField[]>([])
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [showResetConfirm, setShowResetConfirm] = useState(false)

  // ── load ──
  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    const r = await api.getSettings().catch(() => ({ status: 0, body: null }))
    if (r.status === 200 && r.body?.fields) {
      setFields(r.body.fields)
      setDraft({})
      setErrors({})
    } else {
      setLoadError('Could not load settings from the engine.')
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Escape to dismiss (unless the reset confirmation is up)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (showResetConfirm) setShowResetConfirm(false)
        else closeSettings()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [closeSettings, showResetConfirm])

  // ── editing ──
  const eff = useCallback(
    (f: SettingsField): unknown => (f.key in draft ? draft[f.key] : f.value),
    [draft]
  )
  const setVal = useCallback((key: string, value: unknown) => {
    setDraft((d) => ({ ...d, [key]: value }))
    setErrors((e) => (e[key] ? { ...e, [key]: '' } : e))
  }, [])

  // Only changed keys go to the server. For secrets: send a new key, or an empty string to
  // clear an already-configured one (empty + not configured is a no-op, so skip it).
  const changed = useMemo<Record<string, unknown>>(() => {
    const out: Record<string, unknown> = {}
    for (const f of fields) {
      if (!(f.key in draft)) continue
      const v = draft[f.key]
      if (f.kind === 'secret') {
        const s = String(v ?? '')
        if (s.trim() !== '' || f.configured) out[f.key] = s
      } else if (v !== f.value) {
        out[f.key] = v
      }
    }
    return out
  }, [fields, draft])

  const changedCount = Object.keys(changed).length

  // ── save / reset ──
  async function save() {
    if (!changedCount || saving) return
    setSaving(true)
    const r = await api.updateSettings(changed)
    setSaving(false)
    if (r.status === 200 && r.body?.fields) {
      setFields(r.body.fields)
      setDraft({})
      setErrors({})
      toast('success', 'Settings saved — applied to the next extraction run.')
      return
    }
    // 400 → map "key: message" to the offending field; otherwise a general toast.
    const detail = (r.body as { detail?: string } | null)?.detail ?? ''
    const m = /^([a-z0-9_]+):\s*([\s\S]*)$/i.exec(detail)
    if (m && fields.some((f) => f.key === m[1])) {
      setErrors((e) => ({ ...e, [m[1]]: m[2] || 'Invalid value.' }))
    } else {
      toast('error', detail || `Could not save settings (${r.status}).`)
    }
  }

  async function doReset() {
    setShowResetConfirm(false)
    setResetting(true)
    const r = await api.resetSettings()
    setResetting(false)
    if (r.status === 200 && r.body?.fields) {
      setFields(r.body.fields)
      setDraft({})
      setErrors({})
      toast('success', 'Settings reset to defaults — applied to the next extraction run.')
    } else {
      toast('error', 'Could not reset settings.')
    }
  }

  // ── derived sections ──
  const basic = fields.filter((f) => !f.advanced)
  const advanced = fields.filter((f) => f.advanced)

  // ── render ──
  return (
    <>
      <div
        className="fixed inset-0 z-[55] flex items-center justify-center bg-black/50 backdrop-blur-sm"
        onClick={() => closeSettings()}
      >
        <div
          className="w-[720px] rounded-2xl bg-panel border border-line shadow-2xl flex flex-col"
          style={{ maxHeight: '88vh' }}
          onClick={(e) => e.stopPropagation()}
        >
          {/* header */}
          <div className="flex items-center gap-2 px-6 pt-5 pb-3 border-b border-line shrink-0">
            <Gear className="w-5 h-5 text-accent" />
            <h2 className="text-base font-semibold">Settings</h2>
            <span className="text-xs text-muted ml-1">Engine configuration</span>
            <button
              onClick={() => closeSettings()}
              className="ml-auto text-muted hover:text-ink text-lg leading-none px-1"
              title="Close"
            >
              ✕
            </button>
          </div>

          {/* body */}
          <div className="flex-1 min-h-0 overflow-auto px-6 py-4">
            {loading ? (
              <div className="py-16 text-center">
                <div className="mx-auto mb-3 h-6 w-6 rounded-full border-2 border-muted border-t-transparent animate-spin" />
                <div className="text-sm text-muted">Loading settings…</div>
              </div>
            ) : loadError ? (
              <div className="py-16 text-center">
                <div className="text-sm text-red-400 mb-3">{loadError}</div>
                <Button variant="subtle" onClick={load}>
                  Retry
                </Button>
              </div>
            ) : (
              <>
                {groupOrder(basic).map((g) => (
                  <GroupBlock
                    key={g}
                    title={g}
                    fields={basic.filter((f) => f.group === g)}
                    eff={eff}
                    setVal={setVal}
                    errors={errors}
                  />
                ))}

                {advanced.length > 0 && (
                  <div className="mt-4 rounded-xl border border-line">
                    <button
                      onClick={() => setAdvancedOpen((v) => !v)}
                      className="w-full flex items-center gap-2 px-4 py-3 text-sm font-medium hover:bg-panel2 rounded-xl transition-colors"
                    >
                      <ChevronDown
                        className={cn('w-4 h-4 transition-transform', advancedOpen && 'rotate-180')}
                      />
                      Advanced
                      <span className="text-xs text-muted font-normal">
                        ({advanced.length} settings)
                      </span>
                    </button>
                    {advancedOpen && (
                      <div className="px-4 pb-2 border-t border-line">
                        {groupOrder(advanced).map((g) => (
                          <GroupBlock
                            key={g}
                            title={g}
                            fields={advanced.filter((f) => f.group === g)}
                            eff={eff}
                            setVal={setVal}
                            errors={errors}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {/* footer */}
          <div className="flex items-center justify-between gap-3 px-6 py-4 border-t border-line shrink-0">
            <Button
              variant="ghost"
              disabled={loading || saving || resetting || !!loadError}
              onClick={() => setShowResetConfirm(true)}
              className="text-red-400 hover:bg-red-400/10"
            >
              {resetting ? 'Resetting…' : 'Reset to defaults'}
            </Button>
            <div className="flex items-center gap-3">
              {changedCount > 0 && (
                <span className="text-xs text-muted">
                  {changedCount} change{changedCount === 1 ? '' : 's'}
                </span>
              )}
              <Button variant="ghost" onClick={() => closeSettings()}>
                Cancel
              </Button>
              <Button disabled={!changedCount || saving || loading} onClick={save}>
                {saving ? 'Saving…' : 'Save changes'}
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* reset confirmation */}
      {showResetConfirm && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div
            className="w-[420px] rounded-2xl bg-panel border border-line shadow-2xl p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold mb-2">Reset all settings?</h3>
            <p className="text-sm text-muted mb-5">
              This clears every override and restores the engine&apos;s defaults — including any
              API key you&apos;ve set here. This cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => setShowResetConfirm(false)}>
                Cancel
              </Button>
              <Button variant="destructive" onClick={doReset}>
                Reset to defaults
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

// ── group block ────────────────────────────────────────────────────────────────

function GroupBlock({
  title,
  fields,
  eff,
  setVal,
  errors
}: {
  title: string
  fields: SettingsField[]
  eff: (f: SettingsField) => unknown
  setVal: (key: string, value: unknown) => void
  errors: Record<string, string>
}) {
  if (!fields.length) return null
  return (
    <div className="mb-5 last:mb-1">
      <div className="text-xs font-semibold text-muted uppercase tracking-wide mb-1 mt-2">
        {title}
      </div>
      <div className="rounded-xl border border-line bg-panel2/40 px-4">
        {fields.map((f) => (
          <FieldRow key={f.key} field={f} value={eff(f)} setVal={setVal} error={errors[f.key]} />
        ))}
      </div>
    </div>
  )
}

// ── field row ────────────────────────────────────────────────────────────────

function FieldRow({
  field,
  value,
  setVal,
  error
}: {
  field: SettingsField
  value: unknown
  setVal: (key: string, value: unknown) => void
  error?: string
}) {
  const warn = usageWarning(field, value)
  return (
    <div className="py-3 border-b border-line/50 last:border-0">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium">{field.label}</span>
            {field.overridden && (
              <span className="inline-flex items-center rounded-full bg-accent/15 text-accent px-2 py-0.5 text-[10px] font-medium">
                Modified
              </span>
            )}
          </div>
          <p className="text-xs text-muted mt-0.5 leading-snug">{field.help}</p>
        </div>
        <div className="shrink-0 w-[210px] flex flex-col items-end gap-1">
          <FieldControl field={field} value={value} setVal={setVal} />
          {field.kind !== 'secret' && (
            <span className="text-[10px] text-muted/80">Default: {fmtDefault(field.default)}</span>
          )}
        </div>
      </div>
      {warn && (
        <p className="mt-2 text-xs text-amber-400 bg-amber-400/10 rounded-md px-2 py-1">{warn}</p>
      )}
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </div>
  )
}

// ── controls by kind ───────────────────────────────────────────────────────────

const inputCls =
  'rounded-lg bg-panel border border-line px-2.5 py-1.5 text-sm focus:outline-none ' +
  'focus:border-accent/60 focus:ring-1 focus:ring-accent/40'

function FieldControl({
  field,
  value,
  setVal
}: {
  field: SettingsField
  value: unknown
  setVal: (key: string, value: unknown) => void
}) {
  const { kind, key } = field

  if (kind === 'bool') {
    const on = value === true
    return (
      <button
        role="switch"
        aria-checked={on}
        onClick={() => setVal(key, !on)}
        className={cn(
          'relative h-6 w-11 rounded-full transition-colors shrink-0',
          on ? 'bg-accent' : 'bg-line'
        )}
      >
        <span
          className={cn(
            'absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white transition-transform',
            on && 'translate-x-5'
          )}
        />
      </button>
    )
  }

  if (kind === 'enum') {
    return (
      <select
        value={String(value ?? '')}
        onChange={(e) => {
          // preserve numeric option types (e.g. DPI 200) so the backend coercion matches
          const raw = e.target.value
          const opt = field.options?.find((o) => String(o) === raw)
          setVal(key, opt ?? raw)
        }}
        className={cn(inputCls, 'w-full')}
      >
        {field.options?.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </select>
    )
  }

  if (kind === 'int' || kind === 'float') {
    const num = typeof value === 'number' ? value : Number(value)
    const step = field.step ?? (kind === 'int' ? 1 : 'any')
    const hasRange = field.minimum != null && field.maximum != null
    const onNum = (raw: string) => {
      if (raw === '') return setVal(key, '')
      const n = kind === 'int' ? Math.round(Number(raw)) : Number(raw)
      if (Number.isFinite(n)) setVal(key, n)
    }
    return (
      <div className="w-full flex items-center gap-2">
        {hasRange && (
          <input
            type="range"
            min={field.minimum ?? undefined}
            max={field.maximum ?? undefined}
            step={step}
            value={Number.isFinite(num) ? num : field.minimum ?? 0}
            onChange={(e) => onNum(e.target.value)}
            className="flex-1 accent-accent"
          />
        )}
        <input
          type="number"
          min={field.minimum ?? undefined}
          max={field.maximum ?? undefined}
          step={step}
          value={Number.isFinite(num) ? num : ''}
          onChange={(e) => onNum(e.target.value)}
          className={cn(inputCls, hasRange ? 'w-16 text-right' : 'w-full text-right')}
        />
      </div>
    )
  }

  if (kind === 'secret') return <SecretControl field={field} value={value} setVal={setVal} />

  // str
  return (
    <input
      type="text"
      value={String(value ?? '')}
      onChange={(e) => setVal(key, e.target.value)}
      className={cn(inputCls, 'w-full')}
    />
  )
}

// secret: password input; never shows the stored value. "Configured ✓" when a key is set and
// untouched; typing sets a new key; Clear stages an empty string (reverts to .env/default).
function SecretControl({
  field,
  value,
  setVal
}: {
  field: SettingsField
  value: unknown
  setVal: (key: string, value: unknown) => void
}) {
  const touched = typeof value === 'string'
  const staged = touched ? (value as string) : ''
  return (
    <div className="w-full">
      <input
        type="password"
        value={staged}
        placeholder={field.configured ? '•••••••• (configured)' : 'Not set'}
        onChange={(e) => setVal(field.key, e.target.value)}
        className={cn(inputCls, 'w-full')}
        autoComplete="off"
      />
      <div className="flex items-center justify-end gap-2 mt-1 h-4">
        {!touched && field.configured && (
          <span className="text-[10px] text-green-400">Configured ✓</span>
        )}
        {touched && staged === '' && field.configured && (
          <span className="text-[10px] text-amber-400">Will clear on save</span>
        )}
        {(field.configured || touched) && (
          <button
            onClick={() => setVal(field.key, '')}
            className="text-[10px] text-muted hover:text-red-400"
          >
            Clear
          </button>
        )}
      </div>
    </div>
  )
}
