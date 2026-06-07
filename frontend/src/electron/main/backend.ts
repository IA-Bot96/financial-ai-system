/**
 * FIE backend sidecar lifecycle (Electron main process).
 *
 * Dev: set FIE_BACKEND_URL to point at an already-running `uvicorn app.main:app`
 *      (simplest), OR let this spawn it via FIE_BACKEND_DIR (the repo's backend/).
 * Prod (Phase 8): swap the spawn command for the bundled PyInstaller exe.
 *
 * All renderer→backend HTTP is proxied through `request()` here (main process), so the
 * sandboxed renderer never makes cross-origin calls — no CORS dependency.
 */
import { spawn, ChildProcess } from 'child_process'
import { createServer } from 'net'
import { createWriteStream } from 'fs'
import { readFile, writeFile } from 'fs/promises'
import { tmpdir } from 'os'
import { basename, join } from 'path'
import { app } from 'electron'

let child: ChildProcess | null = null
let _url = ''
let _logPath = ''

export const backendUrl = (): string => _url
export const backendLogPath = (): string => _logPath

function freePort(): Promise<number> {
  return new Promise((res, rej) => {
    const srv = createServer()
    srv.unref()
    srv.on('error', rej)
    srv.listen(0, '127.0.0.1', () => {
      const port = (srv.address() as { port: number }).port
      srv.close(() => res(port))
    })
  })
}

export async function startBackend(): Promise<string> {
  // Dev convenience: use an already-running backend, skip spawning.
  if (process.env.FIE_BACKEND_URL) {
    _url = process.env.FIE_BACKEND_URL.replace(/\/$/, '')
    return _url
  }

  const port = await freePort()
  _url = `http://127.0.0.1:${port}`
  _logPath = join(app.getPath('userData'), 'backend.log')
  const out = createWriteStream(_logPath, { flags: 'a' })

  // PACKAGED: launch the self-contained PyInstaller bundle shipped in resources/backend
  // (no system Python / tesseract / model download needed). Storage + logs go to a
  // writable per-user dir; run_server.py reads FIE_STORAGE_ROOT and the bundled deps.
  if (app.isPackaged) {
    const exeName = process.platform === 'win32' ? 'aifi-backend.exe' : 'aifi-backend'
    const bin = join(process.resourcesPath, 'backend', exeName)
    const storage = join(app.getPath('userData'), 'backend-storage')
    out.write(`\n[${new Date().toISOString()}] starting bundled backend: ${bin} (port=${port})\n`)
    child = spawn(bin, ['--host', '127.0.0.1', '--port', String(port)], {
      cwd: join(process.resourcesPath, 'backend'),
      env: {
        ...process.env,
        FIE_HOST: '127.0.0.1',
        FIE_PORT: String(port),
        FIE_STORAGE_ROOT: storage
      },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    child.stdout?.pipe(out)
    child.stderr?.pipe(out)
    child.on('exit', (code) => out.write(`\n[backend exited code=${code}]\n`))
    child.on('error', (e) => out.write(`\n[backend spawn error] ${String(e)}\n`))
    return _url
  }

  // DEV: run from the repo's backend dir via the run_server launcher (override with
  // FIE_BACKEND_DIR / FIE_BACKEND_CMD). `python` on Windows is often a Store alias that
  // bare spawn can't exec, so shell:true resolves PATH/aliases like the user's terminal.
  const cwd = process.env.FIE_BACKEND_DIR || join(app.getAppPath(), '..', 'backend')
  const cmd = process.env.FIE_BACKEND_CMD
  const [bin, ...args] = cmd
    ? cmd.split(' ')
    : ['python', 'run_server.py', '--host', '127.0.0.1', '--port', String(port)]

  out.write(`\n[${new Date().toISOString()}] starting backend: ${bin} ${args.join(' ')} (cwd=${cwd})\n`)
  child = spawn(bin, args, {
    cwd,
    env: { ...process.env },
    stdio: ['ignore', 'pipe', 'pipe'],
    shell: process.platform === 'win32'
  })
  child.stdout?.pipe(out)
  child.stderr?.pipe(out)
  child.on('exit', (code) => out.write(`\n[backend exited code=${code}]\n`))
  child.on('error', (e) => out.write(`\n[backend spawn error] ${String(e)}\n`))
  return _url
}

export function stopBackend(): void {
  if (child && !child.killed) {
    child.kill()
    child = null
  }
}

export interface BackendRequest {
  method: string
  path: string
  json?: unknown
}
export interface BackendResponse {
  status: number
  body: unknown
}

/** Proxy a backend call from main (no CORS). Returns status 0 if unreachable. */
export async function request(req: BackendRequest): Promise<BackendResponse> {
  try {
    const init: RequestInit = { method: req.method }
    if (req.json !== undefined) {
      init.body = JSON.stringify(req.json)
      init.headers = { 'Content-Type': 'application/json' }
    }
    const r = await fetch(_url + req.path, init)
    const body = await r.json().catch(() => null)
    return { status: r.status, body }
  } catch {
    return { status: 0, body: null } // connection refused / not up yet
  }
}

/** Upload a workbook file to a FIE endpoint as multipart (used for session create/reload). */
export async function uploadWorkbook(path: string, endpoint: string): Promise<BackendResponse> {
  try {
    const buf = await readFile(path)
    const fd = new FormData()
    fd.append('file', new Blob([buf]), basename(path))
    const r = await fetch(_url + endpoint, { method: 'POST', body: fd })
    return { status: r.status, body: await r.json().catch(() => null) }
  } catch (e) {
    return { status: 0, body: { detail: String(e) } }
  }
}

/** Start an extraction job: upload PDFs as multipart `files[]` to /api/extraction/jobs.
 *  If `templatePath` is provided the Excel file is attached as the `template` field,
 *  enabling template-driven P&L / BS mapping instead of the default no_template mode. */
export async function createExtractionJob(
  paths: string[],
  templatePath?: string
): Promise<BackendResponse> {
  try {
    const fd = new FormData()
    for (const p of paths) {
      const buf = await readFile(p)
      fd.append('files', new Blob([buf]), basename(p))
    }
    if (templatePath) {
      const buf = await readFile(templatePath)
      fd.append('template', new Blob([buf]), basename(templatePath))
    }
    const r = await fetch(_url + '/api/extraction/jobs', { method: 'POST', body: fd })
    return { status: r.status, body: await r.json().catch(() => null) }
  } catch (e) {
    return { status: 0, body: { detail: String(e) } }
  }
}

/** Signal the backend to cancel an in-progress extraction job.
 *  Returns immediately with { status: "cancelling" } — the worker stops cooperatively.
 *  Caller must keep polling until a terminal status is observed. */
export async function cancelExtractionJob(jobId: string): Promise<BackendResponse> {
  try {
    const r = await fetch(`${_url}/api/extraction/jobs/${jobId}`, { method: 'DELETE' })
    return { status: r.status, body: await r.json().catch(() => null) }
  } catch (e) {
    return { status: 0, body: { detail: String(e) } }
  }
}

/** Download a finished job's xlsx, persist it, and ingest it as a FIE session. */
export async function ingestJobResult(
  jobId: string
): Promise<BackendResponse & { path?: string }> {
  try {
    const dl = await fetch(`${_url}/api/extraction/jobs/${jobId}/download`)
    if (!dl.ok) return { status: dl.status, body: { detail: 'download failed' } }
    const tmp = join(tmpdir(), `fie-${jobId}.xlsx`)
    await writeFile(tmp, Buffer.from(await dl.arrayBuffer()))
    const up = await uploadWorkbook(tmp, '/api/fie/sessions')
    return { ...up, path: tmp }
  } catch (e) {
    return { status: 0, body: { detail: String(e) } }
  }
}
