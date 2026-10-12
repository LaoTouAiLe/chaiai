// 「页面只有一条通道」的守卫（v0.8 T75 验收②、§4 硬验收 3）。
//
// 这一条不是形式主义：v0.4 页面与 v0.8 增强共用**同一套应用服务**，靠的就是"所有请求都经
// `src/api` 下的相对 `/api` 路径"。一旦有人图省事在某个组件里直接 `fetch('https://...')`，
// 那就等于凭空多出一条绕过权限、审批与工作区边界的通道——而那种改动在评审里很难被看见。
//
// 所以这里**扫源码**：允许发请求的文件只有 `client.ts`（统一请求器）与 `events.ts`（SSE 订阅），
// 且这两个文件里不许出现写死的绝对地址。新增通道必须先改这条用例，改用例就得说出理由。

import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

/** 唯一允许发请求的两个文件（前端目录相对路径，``/`` 分隔）。 */
const TRANSPORT_FILES = ['src/api/client.ts', 'src/api/events.ts']

/** 发请求的三种写法：够用了——多标一种不会漏掉真货，只会让漏网的写法更显眼。 */
const REQUEST_PATTERN = /\bfetch\(|new EventSource|XMLHttpRequest/

/** 展示用的原型夹具不参与：它们里面的 `https://` 是给人看的假数据，不是请求。 */
const EXCLUDED_PREFIXES = ['src/fixtures/']

function sourceFiles(directory: string): string[] {
  const found: string[] = []
  for (const entry of readdirSync(directory)) {
    const full = join(directory, entry)
    if (statSync(full).isDirectory()) {
      found.push(...sourceFiles(full))
      continue
    }
    if (!/\.tsx?$/.test(entry)) continue
    if (/\.test\.tsx?$/.test(entry)) continue
    // 统一成前端目录相对路径（`src/api/client.ts` 这种），与 `TRANSPORT_FILES` 同一口径。
    found.push(relative('.', full).split(sep).join('/'))
  }
  return found
}

describe('页面的通道（T75）', () => {
  it('sends every request through the one shared transport', () => {
    const offenders = sourceFiles('src')
      .filter((file) => !TRANSPORT_FILES.includes(file))
      .filter((file) => !EXCLUDED_PREFIXES.some((prefix) => file.startsWith(prefix)))
      .filter((file) => REQUEST_PATTERN.test(readFileSync(file, 'utf8')))

    expect(offenders).toEqual([])
  })

  it('keeps the transport on relative paths with no hardcoded endpoint', () => {
    for (const file of TRANSPORT_FILES) {
      const source = readFileSync(file, 'utf8')
      // 写死的外部地址 = 绕过本机应用服务（也就绕过了它的权限与审批）。
      expect(source).not.toMatch(/["'`]https?:\/\//)
    }
  })
})
