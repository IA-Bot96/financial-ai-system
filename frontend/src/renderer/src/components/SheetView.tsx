import { useEffect, useRef, useState } from 'react'
import { createUniver, defaultTheme, LocaleType, merge } from '@univerjs/presets'
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core'
import { CustomCommandExecutionError, ICommandService, IUndoRedoService } from '@univerjs/core'
import * as enUSns from '@univerjs/preset-sheets-core/locales/en-US'
import '@univerjs/preset-sheets-core/lib/index.css'
import { useApp } from '@/store'
import { toUniverData } from '@/lib/sheetjs'
import { setSheetApi } from '@/lib/sheetApi'
import { buildColorMap, type ValidationIssue } from '@/lib/validation'
import { HISTORY_SHEET } from '@/lib/history'
import { ValidationCard } from './ValidationTooltip'

/** 0-based row/col → A1 (e.g. 0,2 → "C1"). */
function rcToA1(row: number, col: number): string {
  let s = ''
  let n = col + 1
  while (n > 0) {
    const m = (n - 1) % 26
    s = String.fromCharCode(65 + m) + s
    n = Math.floor((n - 1) / 26)
  }
  return `${s}${row + 1}`
}

// locale module may expose the bundle as default or as the namespace itself
const sheetsEnUS = (enUSns as { default?: unknown }).default ?? enUSns

/**
 * Structural commands that change the grid TOPOLOGY (insert/remove/move rows or columns,
 * add/remove/rename/reorder sheets). These are vetoed.
 *
 * Rationale: the lossless save is a per-coordinate value diff, which is only sound under a
 * FIXED topology. A mid-sheet row insert, for example, shifts every cell below it down one
 * row in Univer's snapshot while the original XML's rows do NOT shift — so the diff would
 * write shifted values at unshifted coordinates and (because formula cells are skipped)
 * leave formulas summing the wrong cells. The result opens fine and is silently wrong — the
 * worst outcome for a financial statement. So we make topology changes impossible at the
 * command layer (covers toolbar, context menu, keyboard, and programmatic dispatch alike)
 * rather than warning after the fact. Cell-VALUE edits, width/height, styling are unaffected.
 */
const BLOCKED_STRUCTURAL_CMD =
  /(?:insert|remove|delete|move)-(?:row|col|column)s?\b|(?:insert|remove|delete)-sheet\b|set-worksheet-(?:order|name)\b/i

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
  const validationLedger = useApp((s) => s.validationLedger)
  const validationEnabled = useApp((s) => s.validationEnabled)
  const showValidation = useApp((s) => s.showValidation)
  const setManualVerified = useApp((s) => s.setManualVerified)
  const hostRef = useRef<HTMLDivElement>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const apiRef = useRef<unknown>(null)
  const mouse = useRef({ x: 0, y: 0 })
  // one card surface: `pinned` (click) is interactive; otherwise it follows the cursor (hover)
  const [tip, setTip] = useState<{ issue: ValidationIssue; x: number; y: number; pinned: boolean } | null>(
    null
  )
  // undo-stack depth that corresponds to the last saved/loaded state, and the live depth.
  const baselineUndos = useRef(0)
  const liveUndos = useRef(0)

  // include loadSeq so an explicit reload (e.g. Discard) remounts the grid even when the
  // sheet names are unchanged — otherwise discarded edits would linger in the view. Include
  // showValidation so toggling the overlay rebuilds the grid with/without the tinted cells.
  const visibleKey =
    `${loadSeq}:${validationEnabled ? 1 : 0}${showValidation ? 1 : 0}:${sheets.map((s) => s.name).join('|')}`

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    const visible = sheets // load the whole workbook (all sheets)
    // render-only validation tints (sheet → A1 → colour); undefined when toggled off / absent.
    // verified rows resolve to green here (buildColorMap applies the override).
    const valStyles =
      validationEnabled && showValidation && validationLedger
        ? buildColorMap(validationLedger)
        : undefined
    // empty workbook on first load (no session) so an empty grid shows behind the modal
    const data = visible.length
      ? toUniverData(visible, valStyles)
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

    // Ensure the Validation Ledger has a "Manually Verified" column so the checkbox can persist.
    // Written here (before the undo baseline is captured) so merely opening a file isn't marked
    // dirty; the value-diff save still emits it because the parsed baseline lacks the column.
    if (validationEnabled && validationLedger?.mvNeedsHeader) {
      try {
        ;(
          univerAPI as unknown as {
            getActiveWorkbook?: () => {
              getSheetByName?: (n: string) => { getRange?: (a1: string) => { setValue?: (v: unknown) => void } | null } | null
            }
          }
        )
          .getActiveWorkbook?.()
          ?.getSheetByName?.(validationLedger.ledgerSheetName)
          ?.getRange?.(`${validationLedger.mvCell}1`)
          ?.setValue?.('Manually Verified')
      } catch (e) {
        console.error('[sheet] Manually Verified header write failed', e)
      }
    }

    // Hard-disable topology-changing commands (see BLOCKED_STRUCTURAL_CMD). Throwing in a
    // before-command listener vetoes the command from EVERY entry point. A throttled toast
    // explains why so the user isn't left with a silently dead button.
    let cmdSub: { dispose: () => void } | null = null
    let editSub: { dispose: () => void } | null = null
    try {
      const commandService = (
        univer as unknown as { __getInjector: () => { get: (t: unknown) => unknown } }
      ).__getInjector().get(ICommandService) as {
        beforeCommandExecuted: (cb: (cmd: { id?: string }) => void) => { dispose: () => void }
        onCommandExecuted: (
          cb: (cmd: { id?: string; params?: unknown }) => void
        ) => { dispose: () => void }
      }
      let lastToast = 0
      cmdSub = commandService.beforeCommandExecuted((cmd) => {
        if (!cmd?.id) return
        // The History sheet is the app's read-only change log — only the save patch writes it,
        // never the grid. Block value edits/clears while it's the active sheet.
        const editingHistory =
          /set-range-values|set-cell|clear-selection-content/i.test(cmd.id) &&
          useApp.getState().activeSheet === HISTORY_SHEET
        if (!BLOCKED_STRUCTURAL_CMD.test(cmd.id) && !editingHistory) return
        const now = Date.now()
        if (now - lastToast > 1500) {
          useApp
            .getState()
            .toast(
              'info',
              editingHistory
                ? 'The History sheet is a read-only change log maintained by the app.'
                : 'Inserting, deleting, moving, or renaming rows, columns, or sheets is disabled — ' +
                    'the workbook must keep the structure the extraction pipeline produced.'
            )
          lastToast = now
        }
        // Veto: before-listeners run BEFORE the command's _execute (verified in
        // @univerjs/core CommandService.executeCommand/syncExecuteCommand), so throwing here
        // aborts the mutation outright — the grid topology never changes. We throw
        // CustomCommandExecutionError specifically: the command service catches that type
        // and returns false (a clean cancel), whereas a plain Error would re-throw and
        // surface as an unhandled rejection in the toolbar/keyboard caller.
        throw new CustomCommandExecutionError(`command blocked: ${cmd.id}`)
      })
      // Best-effort: stamp the edited cell's time so history windows ("last 5 min") are
      // accurate. Reads the cell from the command's range params; if the param shape differs
      // across Univer versions this silently no-ops and timestamps fall back to save time.
      // A one-shot diagnostic (per mount) reports whether the capture path actually works.
      let stampDiag = false
      editSub = commandService.onCommandExecuted((cmd) => {
        if (!cmd?.id || !/set-range-values/i.test(cmd.id)) return
        const sheet = useApp.getState().activeSheet
        if (!sheet || sheet === HISTORY_SHEET) return
        // Reactive history: tick editSeq for EVERY edit (independent of the best-effort range
        // read below), so the History panel recomputes from the live snapshot in real time.
        useApp.getState().noteGridEdit()
        try {
          const r = (cmd.params as { range?: { startRow?: number; startColumn?: number } })?.range
          if (r && typeof r.startRow === 'number' && typeof r.startColumn === 'number') {
            useApp.getState().markEdit(sheet, rcToA1(r.startRow, r.startColumn))
            if (!stampDiag) {
              console.debug('[history] edit-time capture active (cell read from command params)')
              stampDiag = true
            }
          } else if (!stampDiag) {
            console.warn(
              '[history] edit-time capture: command has no readable range — timestamps will fall ' +
                'back to save time. cmd.id=' + cmd.id,
              cmd.params
            )
            stampDiag = true
          }
        } catch {
          /* best-effort — never block an edit */
        }
      })
    } catch (e) {
      // If the guard can't be installed, fail safe by NOT silently allowing structural edits
      // to corrupt the save: surface it so it's caught rather than shipping a broken save.
      console.error('[sheet] structural-command guard unavailable', e)
      toast('warning', 'Editor safety guard failed to load — avoid inserting/deleting rows or sheets.')
    }

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
        setTip(null) // a pinned/hover validation card belongs to the old sheet — close it
      })
    } catch (e) {
      // sheet-sync is non-essential; never block the grid from rendering
      console.error('[sheet] active-sheet tracking unavailable', e)
    }
    // seed the initial active sheet (the event may not fire for the first sheet on mount).
    // Preserve the sheet the user was on across a validation-toggle remount.
    if (visible.length) {
      const want = useApp.getState().activeSheet
      const target = want && visible.some((s) => s.name === want) ? want : visible[0].name
      if (target !== visible[0].name) {
        try {
          ;(
            univerAPI as unknown as {
              getActiveWorkbook?: () => { getSheetByName?: (n: string) => { activate?: () => void } | null }
            }
          ).getActiveWorkbook?.()?.getSheetByName?.(target)?.activate?.()
        } catch {
          /* non-fatal */
        }
      }
      useApp.getState().setActiveSheet(target)
    }

    // Validation card: HOVER a flagged cell → read-only card following the cursor; CLICK →
    // pin it (the Manually Verified checkbox becomes live). Guarded so missing events never
    // block the grid. Position comes from the wrapper's mousemove (no canvas geometry needed).
    const issueAt = (p: { row?: number; column?: number; worksheet?: { getSheetName?: () => string } }) => {
      const st = useApp.getState()
      if (!st.validationLedger || p?.row == null || p?.column == null) return null
      const sheetName = p.worksheet?.getSheetName?.() ?? st.activeSheet ?? ''
      return st.validationLedger.cellIssue[sheetName]?.[rcToA1(p.row, p.column)] ?? null
    }
    let hoverSub: { dispose: () => void } | null = null
    let clickSub: { dispose: () => void } | null = null
    try {
      const evt = univerAPI as unknown as {
        Event?: { CellHover?: string; CellClicked?: string }
        addEvent?: (e: string, cb: (p: never) => void) => { dispose: () => void }
      }
      if (evt.Event?.CellHover && evt.addEvent) {
        hoverSub = evt.addEvent(evt.Event.CellHover, ((p: { row?: number; column?: number; worksheet?: { getSheetName?: () => string } }) => {
          const st = useApp.getState()
          if (!st.validationEnabled || !st.showValidation) return
          setTip((cur) => {
            if (cur?.pinned) return cur // don't override a pinned (interactive) card
            const issue = issueAt(p)
            return issue ? { issue, x: mouse.current.x, y: mouse.current.y, pinned: false } : null
          })
        }) as (p: never) => void)
      }
      if (evt.Event?.CellClicked && evt.addEvent) {
        clickSub = evt.addEvent(evt.Event.CellClicked, ((p: { row?: number; column?: number; worksheet?: { getSheetName?: () => string } }) => {
          const st = useApp.getState()
          if (!st.validationEnabled || !st.showValidation) return
          const issue = issueAt(p)
          setTip(issue ? { issue, x: mouse.current.x, y: mouse.current.y, pinned: true } : null)
        }) as (p: never) => void)
      }
    } catch (e) {
      console.error('[sheet] cell-hover/click tracking unavailable', e)
    }

    // Selecting a cell highlights its value on the open PDF page (only while the PDF panel
    // is open; debounced so arrow-key roaming doesn't thrash). Citations also flow through
    // here because they programmatically select the cited cell.
    let selSub: { dispose: () => void } | null = null
    let selTimer: ReturnType<typeof setTimeout> | null = null
    try {
      const evt = univerAPI as unknown as {
        Event: { SelectionChanged: string }
        addEvent: (
          e: string,
          cb: (p: { worksheet?: { getActiveRange?: () => { getValue?: () => unknown } | null } }) => void
        ) => { dispose: () => void }
      }
      selSub = evt.addEvent(evt.Event.SelectionChanged, (params) => {
        if (!useApp.getState().panels.pdf) return // no point unless the PDF is showing
        if (selTimer) clearTimeout(selTimer)
        selTimer = setTimeout(() => {
          try {
            const v = params?.worksheet?.getActiveRange?.()?.getValue?.()
            const term = v == null ? '' : String(v).trim()
            useApp.getState().highlightPdf(term || null)
          } catch {
            /* best-effort */
          }
        }, 150)
      })
    } catch (e) {
      console.error('[sheet] selection tracking unavailable', e)
    }

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
        } else {
          setDirty(status.undos !== baselineUndos.current)
        }
        // keep the Edit menu's Undo/Redo enablement in step with the grid's stacks.
        // Undo is measured against the post-load baseline so a freshly loaded workbook
        // (whose data population sits on the stack) reads as "nothing to undo".
        window.api.setMenuState({
          canUndo: status.undos > baselineUndos.current,
          canRedo: status.redos > 0
        })
      })
    } catch (e) {
      // dirty-tracking is non-essential; never let it block the grid from rendering
      console.error('[sheet] undo/redo dirty tracking unavailable', e)
    }

    // Replay any unsaved "Manually Verified" writes so they survive a highlight-toggle remount.
    // Done AFTER the undo baseline is captured so the replayed edits correctly read as dirty.
    const pending = useApp.getState().verifyWrites
    if (Object.keys(pending).length) {
      try {
        const wb = (univerAPI as unknown as {
          getActiveWorkbook?: () => {
            getSheetByName?: (n: string) => { getRange?: (a1: string) => { setValue?: (v: unknown) => void } | null } | null
          }
        }).getActiveWorkbook?.()
        for (const [key, val] of Object.entries(pending)) {
          const bang = key.indexOf('!')
          if (bang < 0) continue
          wb?.getSheetByName?.(key.slice(0, bang))?.getRange?.(key.slice(bang + 1))?.setValue?.(val)
        }
      } catch (e) {
        console.error('[sheet] verify replay failed', e)
      }
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
        cmdSub?.dispose()
        editSub?.dispose()
      } catch {
        /* noop */
      }
      try {
        activeSub?.dispose()
      } catch {
        /* noop */
      }
      try {
        if (selTimer) clearTimeout(selTimer)
        selSub?.dispose()
      } catch {
        /* noop */
      }
      try {
        hoverSub?.dispose()
        clickSub?.dispose()
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

  // citation → cell: activate the sheet, select the cell, AND scroll it into view.
  useEffect(() => {
    if (!nav.cell) return
    type FSheet = {
      activate?: () => void
      getRange?: (a1: string) => { activate?: () => void } | null
      scrollToCell?: (row: number, column: number, duration?: number) => void
    }
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
      ws?.getRange?.(nav.cell.cell)?.activate?.() // select the cell
      // parse the A1 ref to 0-based row/col and scroll so the selection is visible
      const m = /^([A-Z]+)(\d+)$/.exec(nav.cell.cell.toUpperCase())
      if (m && ws?.scrollToCell) {
        let col = 0
        for (const ch of m[1]) col = col * 26 + (ch.charCodeAt(0) - 64)
        ws.scrollToCell(Number(m[2]) - 1, col - 1)
      }
    } catch {
      toast('info', `${nav.cell.sheet}!${nav.cell.cell}`)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nav.cell?.sheet, nav.cell?.cell, nav.cellSeq])

  // close a pinned validation card on Escape or a click outside the grid (grid clicks are
  // handled by CellClicked, which re-pins on a flagged cell or clears otherwise)
  useEffect(() => {
    if (!tip?.pinned) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setTip(null)
    }
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setTip(null)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onDown)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onDown)
    }
  }, [tip?.pinned])

  // Univer manages its own DOM inside `hostRef`; the validation card is a React sibling under
  // the relative wrapper (React never touches hostRef's children, so no unmount race).
  const b = wrapRef.current?.getBoundingClientRect()
  const cx = tip ? Math.max(4, Math.min(tip.x + 14, (b?.width ?? 4000) - 340)) : 0
  const cy = tip ? Math.max(4, Math.min(tip.y + 14, (b?.height ?? 4000) - 180)) : 0
  return (
    <div
      ref={wrapRef}
      className="relative h-full w-full"
      onMouseMove={(e) => {
        const r = wrapRef.current?.getBoundingClientRect()
        mouse.current = { x: e.clientX - (r?.left ?? 0), y: e.clientY - (r?.top ?? 0) }
        setTip((cur) => (cur && !cur.pinned ? { ...cur, x: mouse.current.x, y: mouse.current.y } : cur))
      }}
      onMouseLeave={() => setTip((cur) => (cur && !cur.pinned ? null : cur))}
    >
      <div ref={hostRef} className="h-full w-full" />
      {tip && (
        <ValidationCard
          issue={tip.issue}
          x={cx}
          y={cy}
          interactive={tip.pinned}
          mvAvailable={!!validationLedger?.mvCell}
          onVerify={(checked) => setManualVerified(tip.issue, checked)}
        />
      )}
    </div>
  )
}
