import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

export default defineConfig({
  testDir: './e2e', testMatch: 'browser-live.spec.ts', timeout: 90_000,
  expect: { timeout: 20_000 }, reporter: [['list']], workers: 1,
  use: { baseURL: 'http://127.0.0.1:8146', locale: 'zh-CN', reducedMotion: 'reduce', trace: 'retain-on-failure' },
  projects: [{ name: 'browser-live', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: 'uv run --project .. python ../scripts/serve_browser_test.py',
    url: 'http://127.0.0.1:8146/api/health', reuseExistingServer: !process.env.CI, timeout: 120_000,
  },
})
