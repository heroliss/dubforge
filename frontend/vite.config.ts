import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期端口由启动脚本通过环境变量传入；直接 npm run dev 时使用默认值。
// 生产期: npm run build 产物由 FastAPI 托管 (见 backend/app/main.py)
// 裸启动与 config.example.yaml 保持一致；非默认端口请显式传入环境变量。
const backendPort = Number(process.env.DUBFORGE_BACKEND_PORT || 8600)
const frontendPort = Number(process.env.DUBFORGE_FRONTEND_PORT || 5273)

// 开发期: 前端热更新, /api 代理到同一轮启动的 FastAPI
// 生产期: npm run build 产物由 FastAPI 托管 (见 backend/app/main.py)
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1', // 显式绑 IPv4 (默认 localhost 在 Node24 下只绑 ::1, 脚本探测/浏览器兼容性差)
    port: frontendPort,
    strictPort: true, // 端口被旧服务占用时直接报错，禁止静默跳到 5274 等端口
    proxy: {
      '/api': `http://127.0.0.1:${backendPort}`,
    },
  },
})
