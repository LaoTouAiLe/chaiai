import { defineConfig, devices } from '@playwright/test'

import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

const PORT = 8125

export default defineConfig({
  testDir: './e2e',
  testMatch: 'conversation-variants.spec.ts',
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
  projects: [{ name: 'variants', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `npm run build && uv run --project .. python ../scripts/serve_fake.py --port ${PORT} --variants`,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
})
