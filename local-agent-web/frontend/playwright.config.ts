import { defineConfig, devices } from '@playwright/test'

import { applyLocalBrowserPath } from './playwright.browsers'

applyLocalBrowserPath()

/**
 * Visual baseline runs against the **built** bundle, never the dev server: the
 * screenshots must be reproducible from a locked dependency set.
 *
 * Every capture pins the same conditions as the T25 reference set (see
 * `local-agent-web/docs/ui-reference/README.md` §2.3): Chinese locale, Shanghai
 * timezone, `deviceScaleFactor=1`, light theme and reduced motion.
 */
const PORT = 4173
const HOST = '127.0.0.1'

export default defineConfig({
  testDir: './e2e',
  // 真后端用例需要假模型服务（`playwright.live|streaming|variants|approval.config.ts` 各自拉起一份）。
  // 默认套件只依赖 node，不能顺手把它们收进来——收了就会以"没有服务"的方式失败（实测 2 条），
  // 而那是配置错误不是产品缺陷。
  testIgnore: [
    '**/conversation-live.spec.ts',
    '**/conversation-streaming.spec.ts',
    '**/conversation-variants.spec.ts',
    '**/approval-live.spec.ts',
    '**/workflow-live.spec.ts',
  ],
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  reporter: [['list']],
  snapshotPathTemplate: '{testDir}/__screenshots__/{projectName}/{arg}{ext}',
  expect: {
    toHaveScreenshot: {
      animations: 'disabled',
      caret: 'hide',
      scale: 'css',
      // Strict comparison: a stale baseline has to fail loudly. The previous
      // 0.002 ratio tolerated ~2000 pixels at 1280×800, which is how a baseline
      // still showing the old composer chip ("模型 A") survived a re-capture —
      // `52/52 passed` alone could not prove the snapshots were current.
      // Consequence: re-capture deliberately after a visual change, scoped to the
      // affected tests — `npx playwright test -g '<test name>' --update-snapshots=all`.
      // `=all` rewrites every compared baseline, including ones that still pass;
      // the default mode only writes snapshots whose diff exceeds the tolerance,
      // which is exactly how the stale one survived. Then look at the new image.
      maxDiffPixelRatio: 0,
    },
  },
  use: {
    baseURL: `http://${HOST}:${PORT}`,
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    colorScheme: 'light',
    reducedMotion: 'reduce',
    deviceScaleFactor: 1,
    viewport: { width: 1440, height: 900 },
    trace: 'off',
  },
  projects: [
    {
      name: '1440x900',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: '1280x800',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
    {
      // 窄窗必须有**自己的项目**：两处已修的溢出（`Composer` 的 `.chip`、`ApprovalCard`）
      // 都只在 640 宽度下越界，而上面两个桌面宽度根本照不到。缺了这一档，"横向不溢出"
      // 的 guard 只覆盖了产品最容易出问题的宽度之外的场景。
      name: '640x800',
      use: { ...devices['Desktop Chrome'], viewport: { width: 640, height: 800 } },
    },
  ],
  webServer: {
    command: `npm run build && npx vite preview --host ${HOST} --port ${PORT} --strictPort`,
    url: `http://${HOST}:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
  },
})
