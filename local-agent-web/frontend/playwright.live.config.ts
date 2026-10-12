import { defineConfig, devices } from '@playwright/test'

import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

/**
 * 真后端端到端（T30）。
 *
 * 与默认配置的区别：这里打的是**真的 Python 服务**（`scripts/serve_fake.py`，注入确定性假模型），
 * 而不是 vite preview 的静态产物。假模型服务自己**同源托管**前端产物——生产就是这个形态（§6.2），
 * 所以只需要一个地址，不需要代理，也不会有"桩返回的数据与真后端不一致"这类假通过。
 *
 * 刻意**不并进默认配置**：默认套件（视觉基线 + 设置页）只依赖 node，不该因为跑一次前端测试就
 * 需要 uv 与后端工程。
 */
const PORT = 8123

export default defineConfig({
  testDir: './e2e',
  testMatch: ['conversation-live.spec.ts', 'capabilities-live.spec.ts'],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [['line']],
  // 与默认配置同一套确定性口径（locale/时区/主题/动画），且同样用 `devices['Desktop Chrome']`
  // ——它走本机装的 Chrome，不需要 Playwright 自带的 headless shell（本机没装，实测会启动失败）。
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    colorScheme: 'light',
    reducedMotion: 'reduce',
    deviceScaleFactor: 1,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'live', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    // 构建一次、起一次真服务；本地重复跑时复用已有实例（`reuseExistingServer`）。
    command: 'npm run build && uv run --project .. python ../scripts/serve_fake.py --port 8123',
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
})
