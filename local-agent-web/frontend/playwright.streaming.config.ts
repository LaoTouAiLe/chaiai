import { defineConfig, devices } from '@playwright/test'

import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

const PORT = 8124
const REPLY = '第一句回答已经开始。这里是持续生成的补充说明，会在后续块到来之前先展示给用户。第二句回答继续显示。这里继续提供更多可读内容，方便观察增量过程。第三句回答会在最后才出现。'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'conversation-streaming.spec.ts',
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
  projects: [{ name: 'streaming', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `npm run build && uv run --project .. python ../scripts/serve_fake.py --port ${PORT} --streaming --command-stream-demo --chunk-delay 0.7 --reply "${REPLY}"`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
})
