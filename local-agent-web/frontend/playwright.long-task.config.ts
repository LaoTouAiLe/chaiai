import { defineConfig } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()
export default defineConfig({
  testDir: './e2e', outputDir: './test-results/long-task-reading', workers: 1,
  globalSetup: './e2e/setup-long-task.ts', timeout: 90_000,
  expect: { timeout: 20_000 }, reporter: [['list']],
  use: { locale: 'zh-CN', timezoneId: 'Asia/Shanghai', viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce', trace: 'off' },
  projects: [
    { name: 'streaming', testMatch: 'conversation-streaming.spec.ts', use: { baseURL: 'http://127.0.0.1:8140' } },
    { name: 'background', testMatch: 'conversation-background.spec.ts', use: { baseURL: 'http://127.0.0.1:8141' } },
    { name: 'reading', testMatch: 'long-task-reading.spec.mjs', use: { baseURL: 'http://127.0.0.1:8142' } },
  ],
})
