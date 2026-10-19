import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../dots/webui',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
})
