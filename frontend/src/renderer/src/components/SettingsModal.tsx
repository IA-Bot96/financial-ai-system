import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type SettingsField, type SettingsGroup } from '@/api'
import { useApp } from '@/store'
import { cn } from '@/lib/util'
import { Button } from './ui/Button'
import { Settings as Gear, ChevronDown } from './ui/icons'

// ── helpers ──────────────────────────────────────────────────────────────────

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
  const setValidationEnabled = useApp((s) => s.setValidationEnabled)

  const [fields, setFields] = useState<SettingsField[]>([])
  const [groups, setGroups] = useState<SettingsGroup[]>([])
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [resetting, setResetting] = useState(false)
  const [showResetConfirm, setShowResetConfirm] = useState(false)

  // Keep the live review-bar gate in sync with the persisted backend setting.
  const syncReview = useCallback(
    (flds: SettingsField[]) => {
      const f = flds.find((x) => x.key === 'validation_review_enabled')
      if (f && typeof f.value === 'boolean') setValidationEnabled(f.value)
    },
    [setValidationEnabled]
  )

  // ── load ──
  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    const r = await api.getSettings().catch(() => ({ status: 0, body: null }))
    if (r.status === 200 && r.body?.fields) {
      setFields(r.body.fields)
      setGroups(r.body.groups ?? [])
      setDraft({})
      setErrors({})
      syncReview(r.body.fields)
    } else {
      setLoadError('Could not load settings from the engine.')
    }
    setLoading(false)
  }, [syncReview])

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
      setGroups(r.body.groups ?? groups)
      setDraft({})
      setErrors({})
      syncReview(r.body.fields)
      toast('success', 'Saved — applies to the next extraction run.')
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
      setGroups(r.body.groups ?? groups)
      setDraft({})
      setErrors({})
      syncReview(r.body.fields)
      toast('success', 'Settings reset to defaults.')
    } else {
      toast('error', 'Could not reset settings.')
    }
  }

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

          {/* body — sections rendered in backend `groups` order; Advanced collapsed */}
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
              groups.map((g) => {
                const groupFields = fields.filter((f) => f.group === g.name)
                if (!groupFields.length) return null
                return (
                  <GroupSection
                    key={g.name}
                    group={g}
                    fields={groupFields}
                    eff={eff}
                    setVal={setVal}
                    errors={errors}
                  />
                )
              })
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

// ── group section (collapsible when group.collapsed; nests subgroups) ──────────

function GroupSection({
  group,
  fields,
  eff,
  setVal,
  errors
}: {
  group: SettingsGroup
  fields: SettingsField[]
  eff: (f: SettingsField) => unknown
  setVal: (key: string, value: unknown) => void
  errors: Record<string, string>
}) {
  const [open, setOpen] = useState(!group.collapsed)
  const direct = fields.filter((f) => !f.subgroup)
  const subgroups = [...new Set(fields.filter((f) => f.subgroup).map((f) => f.subgroup as string))]

  const header = group.collapsed ? (
    <button
      onClick={() => setOpen((v) => !v)}
      className="flex items-center gap-1.5 mb-1 mt-2 text-xs font-semibold text-muted uppercase tracking-wide hover:text-ink transition-colors"
    >
      <ChevronDown className={cn('w-3.5 h-3.5 transition-transform', !open && '-rotate-90')} />
      {group.name}
      <span className="font-normal normal-case text-muted/70">({fields.length})</span>
    </button>
  ) : (
    <div className="text-xs font-semibold text-muted uppercase tracking-wide mb-1 mt-2">
      {group.name}
    </div>
  )

  return (
    <div className="mb-5 last:mb-1">
      {header}
      {open && (
        <div className="rounded-xl border border-line bg-panel2/40 px-4">
          {direct.map((f) => (
            <FieldRow key={f.key} field={f} value={eff(f)} setVal={setVal} error={errors[f.key]} />
          ))}
          {subgroups.map((sn) => (
            <SubgroupBlock
              key={sn}
              name={sn}
              fields={fields.filter((f) => f.subgroup === sn)}
              eff={eff}
              setVal={setVal}
              errors={errors}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// A nested block (e.g. Vision under Extraction). Its leading bool field is the toggle; the
// remaining fields are revealed only when that toggle is on.
function SubgroupBlock({
  name,
  fields,
  eff,
  setVal,
  errors
}: {
  name: string
  fields: SettingsField[]
  eff: (f: SettingsField) => unknown
  setVal: (key: string, value: unknown) => void
  errors: Record<string, string>
}) {
  const toggle = fields.find((f) => f.kind === 'bool')
  const rest = fields.filter((f) => f !== toggle)
  const on = toggle ? eff(toggle) === true : true
  return (
    <div className="my-2 rounded-lg border border-line/60 bg-panel/40 px-3">
      <div className="text-[11px] font-medium text-muted/80 uppercase tracking-wide pt-2 -mb-1">
        {name}
      </div>
      {toggle && (
        <FieldRow field={toggle} value={eff(toggle)} setVal={setVal} error={errors[toggle.key]} />
      )}
      {on &&
        rest.map((f) => (
          <FieldRow key={f.key} field={f} value={eff(f)} setVal={setVal} error={errors[f.key]} />
        ))}
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
            {field.badge && (
              <span className="rounded bg-amber-500/15 text-amber-400 text-[10px] font-semibold px-1.5 py-0.5 tracking-wide">
                {field.badge}
              </span>
            )}
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
            'absolute top-1/2 left-0.5 h-5 w-5 -translate-y-1/2 rounded-full bg-white transition-transform',
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
