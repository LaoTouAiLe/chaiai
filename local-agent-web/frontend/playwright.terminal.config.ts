import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()
const port = 8137
export default defineConfig({
  testDir: './e2e', testMatch: 'terminal-live.spec.ts', workers: 1,
  timeout: 60_000, expect: { timeout: 15_000 }, reporter: [['line']],
  outputDir: process.env.TERMINAL_TEST_OUTPUT ?? 'test-results/terminal',
  use: { baseURL: `http://127.0.0.1:${port}`, locale: 'zh-CN', trace: 'retain-on-failure',
    ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
  webServer: {
    command: `..\\.venv\\Scripts\\python.exe ../scripts/serve_fake.py --port ${port} --dist frontend/dist`,
    url: `http://127.0.0.1:${port}/api/health`, timeout: 60_000, reuseExistingServer: false,
  },
})
