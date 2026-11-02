import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/next/',
  plugins: [vue()],
  cacheDir: '../.cache/vite',
  build: { outDir: '../static/next', emptyOutDir: true, sourcemap: false },
  server: { host: '127.0.0.1', port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8765' } },
})
