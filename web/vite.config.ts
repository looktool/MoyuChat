import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 浏览器无法直接建裸 TCP 连接，因此前端通过 WebSocket 连到 ws_bridge.py，
// 由它把 JSON 报文转成服务器要求的「4 字节长度前缀 + JSON」TCP 帧。
export default defineConfig({
  plugins: [react()],
  server: {
    // host: true → 监听所有网卡，局域网内其他人可用 http://<服务器IP>:5173 访问
    host: true,
    port: 5173,
    proxy: {
      '/ws': {
        // 桥接地址可用 VITE_WS_TARGET 覆盖（默认本机桥接 9106）
        target: process.env.VITE_WS_TARGET || 'ws://127.0.0.1:9106',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
