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
        ws: true,
      },
    },
  },
  /**
   * 显式清空 preview 的 `/api` 代理（2026-10-30）。
   *
   * `preview.proxy` 默认**继承** `server.proxy`（Vite 文档）。而 e2e 的视觉基线用
   * `vite preview` 起静态站，于是每一屏页面的 `/api` 请求都被代理到
   * `127.0.0.1:8000` 的**真实服务**——跑一次 e2e 就在真实数据里造出一批会话
   * （实测：66 个空会话，`matched_turn_ids` 全为 0）。
   *
   * preview 只该发静态产物：清空后请求 `/api` 直接 404，前端按"没有后端"回落，
   * 这正是默认套件（只依赖 node）想要的形态。需要真后端的用例走各自的
   * `playwright.*-live.config.ts`，它们自己拉起假模型服务、用隔离数据目录。
   */
  preview: {
    proxy: {},
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
