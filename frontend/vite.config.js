import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The Starlette SSE bridge runs on :8080 (uvicorn api.server:app --port 8080).
// In dev, all /api calls are proxied to it so the browser stays same-origin.
// In production the bridge serves the built dist/, so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.BRIDGE_URL || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
