import { defineConfig } from '@playwright/test'
import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

export default defineConfig({
  testDir: './e2e',
  testMatch: 'question-queue-live.spec.mjs',
  outputDir: './test-results/question-queue',
  workers: 1,
  timeout: 60_000,
  reporter: [['list']],
  globalSetup: './e2e/setup-question-queue.ts',
  use: {
    baseURL: 'http://127.0.0.1:8131',
    actionTimeout: 10_000,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
    viewport: { width: 1440, height: 900 },
    // Long-lived SSE responses are covered by explicit HTTP assertions and screenshots.
    trace: 'off',
  },
})
