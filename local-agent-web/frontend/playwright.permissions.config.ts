import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

const PORT = 8138
const python = process.platform === 'win32' ? '..\\.venv\\Scripts\\python.exe' : '../.venv/bin/python'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'permissions-live.spec.ts',
  outputDir: './test-results/permissions',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [['line']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce', trace: 'retain-on-failure',
  },
  projects: [{ name: 'permissions', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: {
    command: `npm run build && "${python}" ../scripts/serve_fake.py --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/health`, reuseExistingServer: false, timeout: 240_000,
  },
})
