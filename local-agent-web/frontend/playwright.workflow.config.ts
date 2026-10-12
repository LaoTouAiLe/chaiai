import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()
const python = process.platform === 'win32' ? '"..\\.venv\\Scripts\\python.exe"' : '../.venv/bin/python'
export default defineConfig({
  testDir: './e2e', testMatch: 'workflow-live.spec.ts', workers: 1,
  timeout: 90_000, expect: { timeout: 20_000 }, reporter: [['line']],
  use: { baseURL: 'http://127.0.0.1:8147', locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce', trace: 'off', screenshot: 'only-on-failure', ...devices['Desktop Chrome'] },
  webServer: {
    command: `npm run build && ${python} ../scripts/serve_workflow_fake.py --port 8147`,
    url: 'http://127.0.0.1:8147/api/health', reuseExistingServer: false, timeout: 240_000,
  },
})
