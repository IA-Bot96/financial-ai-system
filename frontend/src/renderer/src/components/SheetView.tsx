import { useEffect, useRef } from 'react'
import { createUniver, defaultTheme, LocaleType, merge } from '@univerjs/presets'
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core'
import { IUndoRedoService } from '@univerjs/core'
import * as enUSns from '@univerjs/preset-sheets-core/locales/en-US'
import '@univerjs/preset-sheets-core/lib/index.css'
import { useApp } from '@/store'
import { toUniverData } from '@/lib/sheetjs'
import { setSheetApi } from '@/lib/sheetApi'

// locale module may expose the bundle as default or as the namespace itself
const sheetsEnUS = (enUSns as { default?: unknown }).default ?? enUSns

/**
 * Univer grid (client-side render of the parsed workbook). All sheets are loaded.
 * Dirty state is derived from Univer's own undo/redo stack so the top indicator bar
 * tracks edits, in-grid undo (Ctrl+Z) and redo (Ctrl+Y), and clears on save.
 * Re-mounts when the visible sheet set changes or on an explicit reload.
 */
export function SheetView() {
  const sheets = useApp((s) => s.sheets)
  const loadSeq = useApp((s) => s.loadSeq)
  const cleanToken = useApp((s) => s.cleanToken)
  const setDirty = useApp((s) => s.setDirty)
  const nav = useApp((s) => s.nav)
  const toast = useApp((s) => s.toast)
  const hostRef = useRef<HTMLDivElement>(null)
  const apiRef = useRef<unknown>(null)
  // undo-stack depth that corresponds to the last saved/loaded state, and the live depth.
  const baselineUndos = useRef(0)
  const liveUndos = useRef(0)

  // include loadSeq so an explicit reload (e.g. Discard) remounts the grid even when the
  // sheet names are unchanged — otherwise discarded edits would linger in the view.
  const visibleKey = `${loadSeq}:${sheets.map((s) => s.name).join('|')}`

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const visible = sheets // load the whole workbook (all sheets)
    // empty workbook on first load (no session) so an empty grid shows behind the modal
    const data = visible.length
      ? toUniverData(visible)
      : {
          id: 'fie-empty',
          name: 'workbook',
          sheetOrder: ['s0'],
          sheets: { s0: { id: 's0', name: 'Sheet1', cellData: {}, rowCount: 100, columnCount: 26 } }
        }

    // Mount Univer into its OWN child element (Univer manages this DOM with its own React
    // root). React never touches it, so we avoid the removeChild/unmount-during-render race.
    const container = document.createElement('div')
    container.style.width = '100%'
    container.style.height = '100%'
    host.appendChild(container)

    const { univer, univerAPI } = createUniver({
      locale: LocaleType.EN_US,
      locales: { [LocaleType.EN_US]: merge({}, sheetsEnUS as Record<string, unknown>) },
      theme: defaultTheme,
      presets: [UniverSheetsCorePreset({ container })]
    })
    univerAPI.createWorkbook(data as never)
    apiRef.current = univerAPI
    setSheetApi(univerAPI)

    // Track the active worksheet so the PDF viewer can sync to each sheet's source page.
    let activeSub: { dispose: () => void } | null = null
    try {
      const evt = (univerAPI as unknown as {
        Event: { ActiveSheetChanged: string }
        addEvent: (
          e: string,
          cb: (p: { activeSheet?: { getSheetName?: () => string } }) => void
        ) => { dispose: () => void }
      })
      activeSub = evt.addEvent(evt.Event.ActiveSheetChanged, (params) => {
        const name = params?.activeSheet?.getSheetName?.()
        if (name) useApp.getState().setActiveSheet(name)
      })
    } catch (e) {
      // sheet-sync is non-essential; never block the grid from rendering
      console.error('[sheet] active-sheet tracking unavailable', e)
    }
    // seed the initial active sheet (the event may not fire for the first sheet on mount)
    if (visible.length) useApp.getState().setActiveSheet(visible[0].name)

    // Derive dirty from Univer's undo/redo stack: dirty when the current undo depth
    // differs from the depth at the last save/load. This makes the top bar respond to
    // edits, in-grid undo (back to baseline => clean) and redo (=> dirty again) — all of
    // which flow through the same stack regardless of how they were triggered.
    baselineUndos.current = 0
    liveUndos.current = 0
    let first = true
    let usub: { unsubscribe: () => void } | null = null
    try {
      const undoRedo = (
        univer as unknown as { __getInjector: () => { get: (t: unknown) => unknown } }
      ).__getInjector().get(IUndoRedoService) as {
        undoRedoStatus$: {
          subscribe: (cb: (s: { undos: number; redos: number }) => void) => {
            unsubscribe: () => void
          }
        }
      }
      usub = undoRedo.undoRedoStatus$.subscribe((status) => {
        liveUndos.current = status.undos
        if (first) {
          baselineUndos.current = status.undos // baseline of the freshly loaded workbook
          first = false
          return
        }
        setDirty(status.undos !== baselineUndos.current)
      })
    } catch (e) {
      // dirty-tracking is non-essential; never let it block the grid from rendering
      console.error('[sheet] undo/redo dirty tracking unavailable', e)
    }

    return () => {
      apiRef.current = null
      setSheetApi(null)
      try {
        usub?.unsubscribe()
      } catch {
        /* noop */
      }
      try {
        activeSub?.dispose()
      } catch {
        /* noop */
      }
      // defer disposal so Univer's React-root unmount doesn't run during React's render
      // phase (StrictMode double-mount) — that caused "unmount a root while rendering".
      setTimeout(() => {
        try {
          univer.dispose()
        } catch {
          /* noop */
        }
        try {
          container.remove()
        } catch {
          /* noop */
        }
      }, 0)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [visibleKey])

  // a successful save (store bumps cleanToken) makes the current state the new baseline,
  // so the indicator stays hidden until the next edit — and undoing past the save point
  // (depth < baseline) correctly re-marks dirty.
  useEffect(() => {
    baselineUndos.current = liveUndos.current
    setDirty(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cleanToken])

  // citation → cell: best-effort select/scroll via the Univer Facade (GUI-verify pending)
  useEffect(() => {
    if (!nav.cell) return
    type FSheet = { activate?: () => void; getRange?: (a1: string) => { activate?: () => void } }
    const api = apiRef.current as {
      getActiveWorkbook?: () => {
        getSheetByName?: (n: string) => FSheet | null
        getActiveSheet?: () => FSheet | null
      }
    } | null
    try {
      const wb = api?.getActiveWorkbook?.()
      const ws: FSheet | null | undefined =
        wb?.getSheetByName?.(nav.cell.sheet) ?? wb?.getActiveSheet?.()
      ws?.activate?.()
      const range = ws?.getRange?.(nav.cell.cell)
      range?.activate?.()
    } catch {
      toast('info', `${nav.cell.sheet}!${nav.cell.cell}`)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.cell?.sheet, nav.cell?.cell, nav.seq])

  return <div ref={hostRef} className="h-full w-full" />
}
