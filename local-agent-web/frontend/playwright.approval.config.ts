import { defineConfig, devices } from '@playwright/test'

import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

/**
 * 审批端到端（T32）。
 *
 * 与默认配置的区别：这里打的是**真的 Python 服务**（`scripts/serve_fake.py --decision tool
 * --approval-edit`，注入确定性假模型），而不是 vite preview 的静态产物。假模型服务自己
 * **同源托管**前端产物——生产就是这个形态（§6.2），所以只需要一个地址。
 *
 * 为什么单独一份配置而不是并进 `playwright.live.config.ts`：那份配置的假模型按普通文本
 * 回答（`decision=chat`），而审批路径需要路由分类为 `tool` 并让模型发出工具调用。两条用例
 * 对同一个进程的要求互斥，共用一个 `webServer` 只会互相污染。
 */
const PORT = 8126

export default defineConfig({
  testDir: './e2e',
  testMatch: 'approval-live.spec.ts',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [['line']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    colorScheme: 'light',
    reducedMotion: 'reduce',
    deviceScaleFactor: 1,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'approval', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    // 构建一次、起一次真服务；本地重复跑时复用已有实例（`reuseExistingServer`）。
    command: `npm run build && uv run --project .. python ../scripts/serve_fake.py --port ${PORT} --decision tool --approval-edit`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
})