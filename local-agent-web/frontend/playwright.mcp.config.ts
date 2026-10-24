import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()
const python = process.platform === 'win32' ? '..\\.venv\\Scripts\\python.exe' : '../.venv/bin/python'

export default defineConfig({
  testDir: './e2e', testMatch: 'mcp-live.spec.ts', workers: 1,
  globalTeardown: './e2e/mcp-teardown.ts',
  timeout: 90_000, expect: { timeout: 20_000 }, reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:8139', locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    actionTimeout: 20_000, reducedMotion: 'reduce', trace: 'off' },
  projects: [
    { name: '1440x900', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: '640x800', use: { ...devices['Desktop Chrome'], viewport: { width: 640, height: 800 } } },
  ],
  webServer: {
    command: `"${python}" ../scripts/serve_mcp_acceptance.py --port 8139`,
    url: 'http://127.0.0.1:8139/api/health', reuseExistingServer: false, timeout: 60_000,
  },
})
