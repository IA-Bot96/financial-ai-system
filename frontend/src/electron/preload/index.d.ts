export interface PickedFile {
  path: string
  name: string
  size: number
}

export interface BackendResponse {
  status: number
  body: unknown
}

export interface FieApi {
  getBackendUrl(): Promise<string>
  getBackendLogPath(): Promise<string>
  request(req: { method: string; path: string; json?: unknown; id?: string }): Promise<BackendResponse>
  cancelRequest(id: string): Promise<void>
  pickFiles(opts?: { extensions?: string[]; multi?: boolean }): Promise<PickedFile[]>
  readFile(path: string): Promise<ArrayBuffer>
  createSession(path: string): Promise<BackendResponse>
  createExtractionJob(paths: string[], templatePath?: string): Promise<BackendResponse>
  cancelExtractionJob(jobId: string): Promise<BackendResponse>
  ingestJobResult(jobId: string): Promise<BackendResponse & { path?: string }>
  saveFile(defaultName: string, data: ArrayBuffer): Promise<string | null>
  openExternal(url: string): Promise<void>
  writeFileAt(path: string, data: ArrayBuffer): Promise<string>
  reloadSession(sessionId: string, path: string): Promise<BackendResponse>
  setDirty(v: boolean): Promise<void>
  setLastFile(p: string | null): Promise<void>
  getLastFile(): Promise<string | null>
  setMenuState(state: {
    view?: string
    hasPdf?: boolean
    hasSession?: boolean
    canUndo?: boolean
    canRedo?: boolean
  }): Promise<void>
  onMenu(cb: (action: string) => void): void
}

declare global {
  interface Window {
    api: FieApi
  }
}
