import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期: 5273 前端热更新, /api 代理到 FastAPI (8600)
// 生产期: npm run build 产物由 FastAPI 托管 (见 backend/app/main.py)
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1', // 显式绑 IPv4 (默认 localhost 在 Node24 下只绑 ::1, 脚本探测/浏览器兼容性差)
    port: 5273,
    proxy: {
      '/api': 'http://127.0.0.1:8600',
    },
  },
})
