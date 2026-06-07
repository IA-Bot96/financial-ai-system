import { app, Menu, BrowserWindow, MenuItemConstructorOptions } from 'electron'

/** Native menu; items send a 'menu:action' IPC the renderer dispatches to store actions. */
export function buildMenu(getWin: () => BrowserWindow | null): void {
  const send = (action: string) => getWin()?.webContents.send('menu:action', action)
  const isMac = process.platform === 'darwin'

  const template: MenuItemConstructorOptions[] = [
    {
      label: 'File',
      submenu: [
        { label: 'Open…', accelerator: 'CmdOrCtrl+O', click: () => send('open') },
        { type: 'separator' },
        { label: 'Save', accelerator: 'CmdOrCtrl+S', click: () => send('save') },
        { label: 'Save As…', accelerator: 'CmdOrCtrl+Shift+S', click: () => send('saveAs') },
        { type: 'separator' },
        isMac ? { role: 'close' } : { role: 'quit' }
      ]
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' }
      ]
    },
    {
      label: 'View',
      submenu: [
        { label: 'Toggle PDF panel', click: () => send('togglePdf') },
        { label: 'Toggle Ask AI', click: () => send('toggleAskAI') },
        { label: 'Dashboard', click: () => send('dashboard') },
        { type: 'separator' },
        { role: 'reload' },
        { role: 'toggleDevTools' }
      ]
    },
    { label: 'Help', submenu: [{ label: `${app.getName()} ${app.getVersion()}`, enabled: false }] }
  ]

  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}
