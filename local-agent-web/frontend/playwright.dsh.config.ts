import { defineConfig } from '@playwright/test'
import base from './playwright.config'

/** Source UI acceptance; screenshots are review artifacts, never golden replacements. */
export default defineConfig({
  ...base,
  testMatch: '**/dsh-workbench.spec.ts',
  testIgnore: [],
  workers: 2,
  timeout: 60_000,
  globalSetup: './e2e/setup-dsh.ts',
  use: { ...base.use, baseURL: 'http://127.0.0.1:4181', reducedMotion: 'reduce' },
  projects: [
    { name: '1440x900', use: { viewport: { width: 1440, height: 900 } } },
    { name: '1280x800', use: { viewport: { width: 1280, height: 800 } } },
    { name: '768x900', use: { viewport: { width: 768, height: 900 } } },
    { name: '390x844', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: undefined,
})
