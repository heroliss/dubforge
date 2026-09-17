import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期: 5273 前端热更新, /api 代理到 FastAPI (8600)
// 生产期: npm run build 产物由 FastAPI 托管 (见 backend/app/main.py)
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5273,
    proxy: {
      '/api': 'http://127.0.0.1:8600',
    },
  },
})
