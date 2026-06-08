/**
 * Module-level handle to the live Univer Facade API so non-Sheet components (Save) can
 * read the current workbook snapshot. Set/cleared by SheetView on mount/unmount.
 */
let _api: unknown = null

export function setSheetApi(api: unknown): void {
  _api = api
}

/** Undo the last grid mutation via Univer's own command stack (the native menu's
 *  webContents.undo() targets the DOM, not Univer, so it's wired here instead). */
export async function undoSheet(): Promise<void> {
  const api = _api as { undo?: () => Promise<boolean> } | null
  try {
    await api?.undo?.()
  } catch (e) {
    console.error('[sheet] undo failed', e)
  }
}

/** Redo the last undone grid mutation via Univer's command stack. */
export async function redoSheet(): Promise<void> {
  const api = _api as { redo?: () => Promise<boolean> } | null
  try {
    await api?.redo?.()
  } catch (e) {
    console.error('[sheet] redo failed', e)
  }
}

type FRange = { setValue?: (v: unknown) => void; setBackgroundColor?: (c: string) => void }
type FSheet = { getRange?: (a1: string) => FRange | null }
type FWorkbook = { getSheetByName?: (n: string) => FSheet | null }

function sheetByName(name: string): FSheet | null {
  const api = _api as { getActiveWorkbook?: () => FWorkbook | null } | null
  try {
    return api?.getActiveWorkbook?.()?.getSheetByName?.(name) ?? null
  } catch {
    return null
  }
}

/** Write a value into a cell via Univer's command stack, so the surgical (value-diff) save
 *  persists it. Used by the "Manually Verified" checkbox to edit the Validation Ledger cell. */
export function writeCell(sheetName: string, a1: string, value: unknown): void {
  try {
    sheetByName(sheetName)?.getRange?.(a1)?.setValue?.(value)
  } catch (e) {
    console.error('[sheet] writeCell failed', e)
  }
}

/** Set a cell's background tint (render-only; the value-diff save ignores styles). Flips a
 *  flagged data cell to/from green when the user toggles "Manually Verified" live. */
export function setCellBackground(sheetName: string, a1: string, color: string): void {
  try {
    sheetByName(sheetName)?.getRange?.(a1)?.setBackgroundColor?.(color)
  } catch (e) {
    console.error('[sheet] setCellBackground failed', e)
  }
}

/** Best-effort current-workbook snapshot (IWorkbookData) — null if unavailable. */
export function getSnapshot(): { sheets?: Record<string, { name?: string; cellData?: unknown }> } | null {
  const api = _api as {
    getActiveWorkbook?: () => { getSnapshot?: () => unknown; save?: () => unknown } | null
  } | null
  try {
    const wb = api?.getActiveWorkbook?.()
    const snap = wb?.getSnapshot?.() ?? wb?.save?.()
    return (snap as ReturnType<typeof getSnapshot>) ?? null
  } catch {
    return null
  }
}
