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
}

async function get<T>(path: string): Promise<{ status: number; body: T }> {
  const r = await window.api.request({ method: 'GET', path })
  return { status: r.status, body: r.body as T }
}
async function post<T>(path: string, json: unknown): Promise<{ status: number; body: T }> {
  const r = await window.api.request({ method: 'POST', path, json })
  return { status: r.status, body: r.body as T }
}

export const api = {
  readiness: () => get<Readiness>('/readiness'),
  health: () => get<{ status: string }>('/health'),
  companies: () => get<{ companies: string[]; default: string }>('/api/fie/companies'),
  answer: (
    sessionId: string,
    query: string,
    history: Array<{ role: string; text: string; frame?: Record<string, unknown> }> = []
  ) => post<FieResponse>(`/api/fie/sessions/${sessionId}/answer`, { query, history }),
  series: (sessionId: string) =>
    get<SeriesResponse>(`/api/fie/sessions/${sessionId}/series`)
}

export interface SeriesResponse {
  years: number[]
  series: Record<string, Record<string, number | null>>
}
