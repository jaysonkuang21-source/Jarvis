import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

const BACKEND = process.env.JARVIS_BACKEND ?? 'http://127.0.0.1:8756'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // Pin IPv4 loopback so Windows "localhost" (::1-only) binds do not
    // break launchers/proxies that probe http://127.0.0.1:5173.
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      // Proxying in dev keeps the browser same-origin, so SSE behaves the same
      // here as it does inside the Tauri shell.
      '/api': {
        target: BACKEND,
        changeOrigin: true,
        // Buffering would defeat token streaming.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache, no-transform'
          })
        },
      },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
