import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Dev server proxies `/api` to the local FastAPI service so the browser talks to
 * one origin. Production removes the proxy entirely: the Vite build is served by
 * FastAPI itself (V0_4_DEVELOPMENT_PLAN §2).
 */
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
