import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  main: {
    build: { lib: { entry: resolve('src/electron/main/index.ts') } },
    plugins: [externalizeDepsPlugin()]
  },
  preload: {
    build: { lib: { entry: resolve('src/electron/preload/index.ts') } },
    plugins: [externalizeDepsPlugin()]
  },
  renderer: {
    resolve: {
      alias: { '@': resolve('src/renderer/src') }
    },
    plugins: [react()]
  }
})
