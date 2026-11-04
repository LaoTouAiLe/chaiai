import { defineConfig } from '@playwright/test'
import base from './playwright.config'

/** Source UI acceptance; screenshots are review artifacts, never golden replacements. */
export default defineConfig({
  ...base,
  testMatch: '**/work-panel.spec.ts',
  testIgnore: [],
  workers: 1,
  timeout: 30_000,
  outputDir: './test-results/file-preview',
  globalSetup: './e2e/setup-file-preview.ts',
  use: { ...base.use, baseURL: 'http://127.0.0.1:4182', reducedMotion: 'reduce' },
  projects: [
    { name: '1440x900', use: { viewport: { width: 1440, height: 900 } } },
    { name: '1280x800', use: { viewport: { width: 1280, height: 800 } } },
    { name: '768x900', use: { viewport: { width: 768, height: 900 } } },
    { name: '390x844', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: undefined,
})
