import { app } from 'electron'
import { readFileSync, writeFileSync } from 'fs'
import { join } from 'path'

export interface AppStateFile {
  bounds?: { width: number; height: number; x?: number; y?: number }
  lastFile?: string | null
}

const file = () => join(app.getPath('userData'), 'state.json')

export function readState(): AppStateFile {
  try {
    return JSON.parse(readFileSync(file(), 'utf8'))
  } catch {
    return {}
  }
}

export function writeState(s: AppStateFile): void {
  try {
    writeFileSync(file(), JSON.stringify(s, null, 2), 'utf8')
  } catch {
    /* best-effort; no DB */
  }
}
