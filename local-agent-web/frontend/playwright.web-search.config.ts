import { defineConfig, devices } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

// 先运行 scripts/verify_web_answers.py --live --serve --port 8137。
// 此配置只检查已生成的隔离会话，不再次调用收费模型或搜索 API。
export default defineConfig({
  testDir: './e2e',
  testMatch: 'web-search-live.spec.ts',
  outputDir: './test-results/web-search',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:8137',
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce', trace: 'retain-on-failure',
  },
  projects: [{ name: 'web-search', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
})
