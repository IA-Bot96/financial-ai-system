import { useEffect, useRef } from 'react'
import { createUniver, defaultTheme, LocaleType, merge } from '@univerjs/presets'
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core'
import * as enUSns from '@univerjs/preset-sheets-core/locales/en-US'
import '@univerjs/preset-sheets-core/lib/index.css'
import { useApp } from '@/store'
import { toUniverData } from '@/lib/sheetjs'
import { setSheetApi } from '@/lib/sheetApi'

// locale module may expose the bundle as default or as the namespace itself
const sheetsEnUS = (enUSns as { default?: unknown }).default ?? enUSns

/**
 * Univer grid (client-side render of the parsed workbook). Meta-sheets are excluded
 * unless "Show source sheets" is on. Cell edits flip the dirty flag (top Save bar).
 * Re-mounts when the visible sheet set changes.
 */
export function SheetView() {
  const sheets = useApp((s) => s.sheets)
  const setDirty = useApp((s) => s.setDirty)
  const nav = useApp((s) => s.nav)
  const toast = useApp((s) => s.toast)
  const hostRef = useRef<HTMLDivElement>(null)
  const apiRef = useRef<unknown>(null)

  const visibleKey = sheets.map((s) => s.name).join('|')

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

    // flag dirty on any cell-mutating command
    const sub = (univerAPI as { onCommandExecuted: (cb: (c: { id?: string }) => void) => { dispose?: () => void } })
      .onCommandExecuted((cmd) => {
        if (typeof cmd?.id === 'string' && /set-range-values|set-cell|insert-|remove-|move-/.test(cmd.id)) {
          setDirty(true)
        }
      })

    return () => {
      apiRef.current = null
      setSheetApi(null)
      // defer disposal so Univer's React-root unmount doesn't run during React's render
      // phase (StrictMode double-mount) — that caused "unmount a root while rendering".
      setTimeout(() => {
        try {
          sub?.dispose?.()
        } catch {
          /* noop */
        }
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
