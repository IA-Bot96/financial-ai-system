import { app, Menu, BrowserWindow, MenuItemConstructorOptions } from 'electron'

/** Renderer-pushed app state the View menu adapts to (see 'menu:state' IPC). */
export interface MenuState {
  view: 'home' | 'sheet' | 'dashboard'
  hasPdf: boolean      // at least one PDF loaded for the current workbook
  hasSession: boolean  // a workbook is open (not the home/new screen)
  canUndo: boolean     // the grid's undo stack is non-empty (reported by SheetView)
  canRedo: boolean     // the grid's redo stack is non-empty
}

const DEFAULT_MENU_STATE: MenuState = {
  view: 'home',
  hasPdf: false,
  hasSession: false,
  canUndo: false,
  canRedo: false
}

/**
 * Native menu; items send a 'menu:action' IPC the renderer dispatches to store actions.
 * The View submenu adapts to ``state``: the PDF item becomes "Add PDFs" when none is loaded,
 * the Dashboard item flips to "Sheet" when already on the dashboard, and panel toggles are
 * disabled off the sheet surface (dashboard / home).
 */
export function buildMenu(getWin: () => BrowserWindow | null,
                          state: MenuState = DEFAULT_MENU_STATE): void {
  const send = (action: string) => getWin()?.webContents.send('menu:action', action)
  const isMac = process.platform === 'darwin'
  const onSheet = state.view === 'sheet'

  // PDF: toggle the panel when a PDF is loaded, else offer to add one. Only on the sheet.
  const pdfItem: MenuItemConstructorOptions = state.hasPdf
    ? { label: 'Toggle PDF panel', enabled: onSheet, click: () => send('togglePdf') }
    : { label: 'Add PDFs', enabled: onSheet, click: () => send('addPdfs') }

  // Dashboard <-> Sheet toggle: label reflects where the click takes you; needs an open workbook.
  const viewToggle: MenuItemConstructorOptions = state.view === 'dashboard'
    ? { label: 'Sheet', enabled: state.hasSession, click: () => send('sheet') }
    : { label: 'Dashboard', enabled: state.hasSession, click: () => send('dashboard') }

  const template: MenuItemConstructorOptions[] = [
    {
      label: 'File',
      submenu: [
        { label: 'Open…', accelerator: 'CmdOrCtrl+O', click: () => send('open') },
        { type: 'separator' },
        // Save / Save As only make sense once a workbook is loaded.
        { label: 'Save', accelerator: 'CmdOrCtrl+S', enabled: state.hasSession,
          click: () => send('save') },
        { label: 'Save As…', accelerator: 'CmdOrCtrl+Shift+S', enabled: state.hasSession,
          click: () => send('saveAs') },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' }
      ]
    },
    {
      label: 'Edit',
      // Undo/Redo dispatch to Univer's command stack and reflect its live depth (sheet-only).
      // Cut/Copy/Paste are intentionally omitted — the grid owns Ctrl+X/C/V natively, and
      // routing them through the native menu didn't drive Univer's clipboard reliably.
      submenu: [
        { label: 'Undo', accelerator: 'CmdOrCtrl+Z', enabled: onSheet && state.canUndo,
          click: () => send('undo') },
        { label: 'Redo', accelerator: 'CmdOrCtrl+Y', enabled: onSheet && state.canRedo,
          click: () => send('redo') }
      ]
    },
    {
      label: 'View',
      submenu: [
        pdfItem,
        { label: 'Toggle Ask AI', enabled: onSheet, click: () => send('toggleAskAI') },
        viewToggle,
        { type: 'separator' },
        // Settings (engine config) — always reachable, even before a workbook is open, so the
        // API key / extraction knobs can be set up front.
        { label: 'Settings…', accelerator: 'CmdOrCtrl+,', click: () => send('settings') },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'toggleDevTools' }
      ]
    },
    { label: 'Help', submenu: [{ label: `${app.getName()} ${app.getVersion()}`, enabled: false }] }
  ]

  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}
