/**
 * Module-level handle to the live Univer Facade API so non-Sheet components (Save) can
 * read the current workbook snapshot. Set/cleared by SheetView on mount/unmount.
 */
let _api: unknown = null

export function setSheetApi(api: unknown): void {
  _api = api
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
