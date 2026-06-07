import { contextBridge, ipcRenderer } from 'electron'

const api = {
  getBackendUrl: (): Promise<string> => ipcRenderer.invoke('backend:url'),
  getBackendLogPath: (): Promise<string> => ipcRenderer.invoke('backend:logPath'),
  /** Proxy a backend HTTP call through the main process (no CORS in the renderer). */
  request: (req: { method: string; path: string; json?: unknown }): Promise<{ status: number; body: unknown }> =>
    ipcRenderer.invoke('backend:request', req),
  pickFiles: (opts?: { extensions?: string[]; multi?: boolean }): Promise<
    { path: string; name: string; size: number }[]
  > => ipcRenderer.invoke('dialog:pickFiles', opts),
  readFile: (path: string): Promise<ArrayBuffer> => ipcRenderer.invoke('fs:readFile', path),
  createSession: (path: string): Promise<{ status: number; body: unknown }> =>
    ipcRenderer.invoke('session:create', path),
  createExtractionJob: (paths: string[]): Promise<{ status: number; body: unknown }> =>
    ipcRenderer.invoke('extraction:create', paths),
  ingestJobResult: (jobId: string): Promise<{ status: number; body: unknown; path?: string }> =>
    ipcRenderer.invoke('extraction:ingest', jobId),
  saveFile: (defaultName: string, data: ArrayBuffer): Promise<string | null> =>
    ipcRenderer.invoke('dialog:saveFile', defaultName, data),
  openExternal: (url: string): Promise<void> => ipcRenderer.invoke('shell:openExternal', url),
  writeFileAt: (path: string, data: ArrayBuffer): Promise<string> =>
    ipcRenderer.invoke('fs:writeFileAt', path, data),
  reloadSession: (sessionId: string, path: string): Promise<{ status: number; body: unknown }> =>
    ipcRenderer.invoke('session:reload', sessionId, path),
  setDirty: (v: boolean): Promise<void> => ipcRenderer.invoke('app:setDirty', v),
  setLastFile: (p: string | null): Promise<void> => ipcRenderer.invoke('app:setLastFile', p),
  getLastFile: (): Promise<string | null> => ipcRenderer.invoke('app:getLastFile'),
  onMenu: (cb: (action: string) => void): void => {
    ipcRenderer.removeAllListeners('menu:action')
    ipcRenderer.on('menu:action', (_e, action: string) => cb(action))
  }
}

contextBridge.exposeInMainWorld('api', api)
