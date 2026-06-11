/**
 * Thin renderer-side client. Every call is proxied through the Electron main process
 * (window.api.request) so the renderer makes no cross-origin requests (no CORS).
 * Session/extraction methods are added in later phases.
 */
export interface Readiness {
  status: 'ready' | 'not_ready'
  workbooks: number
  contracts_ok: boolean
  rate_limiter: string
  secrets: Record<string, boolean>
}

export interface Citation {
  ref_id: string
  kind: 'financial' | 'insight' | 'external' | 'forecast'
  display: string
  locator: Record<string, unknown>
}
export interface Conflict {
  type: string
  topic: string
  resolved: boolean
  resolution?: string
  values?: Record<string, unknown>[]
}
export interface EditHistoryItem {
  timestamp: string
  sheet: string
  cell: string
  old?: string
  new?: string
  saved?: boolean
  kind: 'edit' | 'verify'
  verified_sheet?: string | null
  verified_cell?: string | null
}
export interface EditHistory {
  mode: 'list' | 'aggregate' | 'opened' | 'open_count'
  lead: string
  filters?: string[]
  total?: number
  // list
  items?: EditHistoryItem[]
  shown?: number
  // aggregate
  by_sheet?: Record<string, number>
  most?: [string, number] | null
  // opened
  opened_at?: string | null
  // open_count
  open_count?: number
  opens?: string[]
}

/** Per-query LLM token usage + estimated USD cost (rendered as a chip). Null on the deterministic
 *  path (no LLM was consulted). `source='estimated'` ⇒ computed from tokens × a model rate table;
 *  `source='api'` ⇒ a billed figure the provider returned directly (shown as-is, no '≈'). */
export interface UsageCost {
  model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  api_calls: number
  cached_calls: number
  input_usd: number
  output_usd: number
  total_usd: number
  input_rate_per_1m: number
  output_rate_per_1m: number
  source: 'estimated' | 'api'
}

export interface FieResponse {
  direct_answer: string
  key_findings: string[]
  supporting_analysis: string
  citations: Citation[]
  conflicts: Conflict[]
  confidence: { band: 'High' | 'Medium' | 'Low'; score?: number; reasons?: string[] } | null
  coverage: Record<string, unknown>
  prose_source: 'deterministic' | 'llm'
  frame?: Record<string, unknown>  // resolved QueryFrame — echoed back so history can carry it
  edit_history?: EditHistory | null  // structured change log (only for intent=edit_history)
  usage?: UsageCost | null  // token usage + estimated cost; null when no LLM ran
}

async function get<T>(path: string): Promise<{ status: number; body: T }> {
  const r = await window.api.request({ method: 'GET', path })
  return { status: r.status, body: r.body as T }
}
async function post<T>(path: string, json: unknown, id?: string): Promise<{ status: number; body: T }> {
  const r = await window.api.request({ method: 'POST', path, json, ...(id ? { id } : {}) })
  return { status: r.status, body: r.body as T }
}

// ── Settings (engine config) ──────────────────────────────────────────────────
export type SettingKind = 'int' | 'float' | 'bool' | 'enum' | 'str' | 'secret'
export interface SettingsField {
  key: string
  group: string
  label: string
  help: string
  kind: SettingKind
  advanced: boolean
  minimum: number | null
  maximum: number | null
  step: number | null
  options: (string | number)[] | null
  overridden: boolean
  subgroup?: string | null   // nested block within a group (e.g. "Vision" under Extraction)
  badge?: string | null      // small chip next to the label (e.g. "BETA")
  // non-secret fields only:
  value?: unknown
  default?: unknown
  // secret fields only:
  configured?: boolean
}
export interface SettingsGroup {
  name: string
  collapsed: boolean
}
export interface SettingsSnapshot {
  fields: SettingsField[]
  groups: SettingsGroup[]
}

export const api = {
  readiness: () => get<Readiness>('/readiness'),
  health: () => get<{ status: string }>('/health'),
  companies: () => get<{ companies: string[]; default: string }>('/api/fie/companies'),
  answer: (
    sessionId: string,
    query: string,
    history: Array<{ role: string; text: string; frame?: Record<string, unknown> }> = [],
    opts?: {
      client_now?: string
      pending_edits?: Array<{ timestamp: string; sheet: string; cell: string; old: string; new: string }>
      requestId?: string
    }
  ) =>
    post<FieResponse>(
      `/api/fie/sessions/${sessionId}/answer`,
      {
        query,
        history,
        ...(opts?.client_now ? { client_now: opts.client_now } : {}),
        ...(opts?.pending_edits ? { pending_edits: opts.pending_edits } : {})
      },
      opts?.requestId
    ),
  /** Abort an in-flight answer() request issued with the given requestId. */
  cancel: (requestId: string): Promise<void> => window.api.cancelRequest(requestId),
  series: (sessionId: string) =>
    get<SeriesResponse>(`/api/fie/sessions/${sessionId}/series`),
  // settings — read/update/reset engine config. POST returns the same { fields } snapshot;
  // a 400 carries { detail } which the caller maps to the offending field.
  getSettings: () => get<SettingsSnapshot>('/api/settings'),
  updateSettings: (values: Record<string, unknown>) =>
    post<SettingsSnapshot & { detail?: string }>('/api/settings', { values }),
  resetSettings: () => post<SettingsSnapshot>('/api/settings/reset', {})
}

export interface SeriesResponse {
  years: number[]
  series: Record<string, Record<string, number | null>>
}
