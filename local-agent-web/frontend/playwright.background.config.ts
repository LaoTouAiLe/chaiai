import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

const PORT = 8128
const REPLY = '第一句后台回答已经开始。第二句后台回答继续生成。第三句后台回答在最后完成。'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'conversation-background.spec.ts',
  outputDir: './test-results/background',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [['line']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', colorScheme: 'light',
    reducedMotion: 'reduce', deviceScaleFactor: 1, trace: 'retain-on-failure',
  },
  projects: [{ name: 'background', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `npm run build && uv run --project .. python ../scripts/serve_fake.py --port ${PORT} --streaming --background --chunk-delay 1 --reply "${REPLY}"`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
})
