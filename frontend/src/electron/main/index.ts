import { app, BrowserWindow, ipcMain, dialog, shell } from 'electron'
import { join, basename } from 'path'
import { promises as fs } from 'fs'
import { statSync } from 'fs'
import {
  startBackend,
  stopBackend,
  backendUrl,
  backendLogPath,
  request,
  uploadWorkbook,
  createExtractionJob,
  cancelExtractionJob,
  ingestJobResult,
  type BackendRequest
} from './backend'
import { buildMenu } from './menu'
import { readState, writeState } from './statefile'

let win: BrowserWindow | null = null
let dirty = false // mirrored from the renderer for the close guard
let lastFile: string | null = null
let forceQuit = false

// App icon for the title bar / taskbar. Generated from app-icon.svg via `npm run icons`
// into build/ (sibling of out/). Windows prefers the multi-res .ico; others use the PNG.
const appIcon = join(__dirname, '../../build', process.platform === 'win32' ? 'icon.ico' : 'icon.png')

function createWindow(): void {
  const st = readState()
  lastFile = st.lastFile ?? null
  win = new BrowserWindow({
    width: st.bounds?.width ?? 1320,
    height: st.bounds?.height ?? 860,
    x: st.bounds?.x,
    y: st.bounds?.y,
    minWidth: 1024,
    minHeight: 680,
    show: false,
    backgroundColor: '#0f1115',
    icon: appIcon,
    autoHideMenuBar: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  })

  win.on('ready-to-show', () => win?.show())

  // unsaved-changes guard on window close
  win.on('close', (e) => {
    if (dirty && !forceQuit && win) {
      e.preventDefault()
      const choice = dialog.showMessageBoxSync(win, {
        type: 'warning',
        buttons: ['Discard & close', 'Cancel'],
        defaultId: 1,
        cancelId: 1,
        message: 'You have unsaved changes. Discard them?'
      })
      if (choice === 0) {
        dirty = false
        forceQuit = true
        win.close()
      }
    } else {
      persist()
    }
  })

  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL) // dev (electron-vite)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html')) // prod
  }
}

function persist(): void {
  if (!win) return
  const b = win.getBounds()
  writeState({ bounds: { width: b.width, height: b.height, x: b.x, y: b.y }, lastFile })
}

app.whenReady().then(async () => {
  await startBackend()

  // backend lifecycle / info
  ipcMain.handle('backend:url', () => backendUrl())
  ipcMain.handle('backend:logPath', () => backendLogPath())
  ipcMain.handle('backend:request', (_e, req: BackendRequest) => request(req))

  // file dialogs + disk I/O
  ipcMain.handle('dialog:pickFiles', async (_e, opts?: { extensions?: string[]; multi?: boolean }) => {
    if (!win) return []
    const filters = opts?.extensions?.length
      ? [{ name: 'Supported', extensions: opts.extensions }]
      : []
    const r = await dialog.showOpenDialog(win, {
      properties: opts?.multi ? ['openFile', 'multiSelections'] : ['openFile'],
      filters
    })
    if (r.canceled) return []
    return r.filePaths.map((p) => ({ path: p, name: basename(p), size: statSync(p).size }))
  })

  // read a file's bytes for client-side parsing (SheetJS in the renderer)
  ipcMain.handle('fs:readFile', async (_e, path: string) => (await fs.readFile(path)).buffer)

  // ingest an opened workbook into a backend FIE session (multipart upload from main)
  ipcMain.handle('session:create', (_e, path: string) =>
    uploadWorkbook(path, '/api/fie/sessions'))

  // OCR/PDF extraction
  ipcMain.handle('extraction:create', (_e, paths: string[], templatePath?: string) =>
    createExtractionJob(paths, templatePath))
  ipcMain.handle('extraction:cancel', (_e, jobId: string) => cancelExtractionJob(jobId))
  ipcMain.handle('extraction:ingest', (_e, jobId: string) => ingestJobResult(jobId))

  // open an external link in the system browser (citation kind = external)
  ipcMain.handle('shell:openExternal', (_e, url: string) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url)
  })

  ipcMain.handle('dialog:saveFile', async (_e, defaultName: string, data: ArrayBuffer) => {
    if (!win) return null
    const r = await dialog.showSaveDialog(win, { defaultPath: defaultName })
    if (r.canceled || !r.filePath) return null
    await fs.writeFile(r.filePath, Buffer.from(data))
    return r.filePath
  })

  // save / reload / recovery
  ipcMain.handle('fs:writeFileAt', async (_e, path: string, data: ArrayBuffer) => {
    await fs.writeFile(path, Buffer.from(data))
    return path
  })
  ipcMain.handle('session:reload', (_e, sessionId: string, path: string) =>
    uploadWorkbook(path, `/api/fie/sessions/${sessionId}/reload`))
  ipcMain.handle('app:setDirty', (_e, v: boolean) => {
    dirty = !!v
  })
  ipcMain.handle('app:setLastFile', (_e, p: string | null) => {
    lastFile = p
    persist()
  })
  ipcMain.handle('app:getLastFile', () => lastFile)

  buildMenu(() => win)
  createWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => stopBackend())
