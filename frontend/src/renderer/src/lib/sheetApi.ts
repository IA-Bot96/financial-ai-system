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
