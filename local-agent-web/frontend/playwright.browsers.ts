import { existsSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Point Playwright at a machine-local browser install when one is declared.
 *
 * The browsers are a machine asset, not a repository asset: keeping their path
 * out of source lets the same config run on a machine whose system drive cannot
 * hold them. `playwright.local.json` is gitignored; the environment variable
 * always wins, so CI can supply its own path.
 *
 * 两个配置都必须调用它（视觉基线那套与真后端那套）：漏掉的话 Playwright 会去
 * `%LOCALAPPDATA%\ms-playwright` 找一个本机根本没装的 headless shell，报
 * "Executable doesn't exist"（实测踩过）。
 */
export function applyLocalBrowserPath(): void {
  if (process.env.PLAYWRIGHT_BROWSERS_PATH !== undefined) return
  const file = join(import.meta.dirname, 'playwright.local.json')
  if (!existsSync(file)) return
  const parsed: unknown = JSON.parse(readFileSync(file, 'utf8'))
  const value = (parsed as { browsersPath?: unknown }).browsersPath
  if (typeof value === 'string' && value.length > 0) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = value
  }
}
