import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { FIELDS } from './PreferenceSection'

/**
 * 设置页的字段表与后端偏好模型的一致性检查（2026-10-26）。
 *
 * 这两份清单都是**手写**的，却没有任何东西保证它们对得上：后端给 `RuntimePreferences` 加一个
 * 可改字段，页面上不会自动出现输入框，用户根本改不了，而所有测试仍然全绿。这类"加了但没接上"
 * 只能靠人肉记忆——本文件把它变成一次失败的测试。
 *
 * 数据来源是 `docs/api/preferences-schema.json`（由 `scripts/export_preferences_schema.py`
 * 反射 `RuntimePreferences` 生成）。它只含**机械信息**：键、输入类型、上下界、枚举候选。
 * 文案（label / note）与条件显示（"选对象存储后才出现端点"）是产品语义，仍只在前端维护。
 */

interface SchemaField {
  key: string
  kind: 'bool' | 'int' | 'float' | 'text' | 'choice'
  options: string[]
  minimum: number | null
  maximum: number | null
  minimum_exclusive: boolean
  maximum_exclusive: boolean
  nullable: boolean
}

interface SchemaGroup {
  group: string
  fields: SchemaField[]
}

// 按工作目录找：vitest 的 cwd 是 `frontend/`（`npm run` 的目录），契约文件在仓库根的
// `docs/api/`，所以要往上两级。用 `import.meta.url` 不行——jsdom 环境下它不是 file: 协议。
const schema = JSON.parse(
  readFileSync(join(process.cwd(), '..', '..', 'docs', 'api', 'preferences-schema.json'), 'utf8'),
) as { version: number; groups: SchemaGroup[] }

function tableOf(group: string) {
  return FIELDS[group as keyof typeof FIELDS]
}

describe('设置页字段表与后端偏好模型一致（2026-10-26）', () => {
  it('is not empty: a blank schema would make every check below pass', () => {
    // 下面三条都是「集合里没有多余/缺少项」——清单被清空或导出失败时它们会全部通过。
    // 这条是那道防线本身的防线。
    expect(schema.version).toBeGreaterThanOrEqual(1)
    expect(schema.groups).toHaveLength(9)
    expect(schema.groups.reduce((total, group) => total + group.fields.length, 0)).toBeGreaterThan(40)
  })

  it('covers every backend field with a form row', () => {
    const missing: string[] = []
    for (const group of schema.groups) {
      const table = tableOf(group.group)
      if (table === undefined) {
        missing.push(`${group.group}（整组缺失）`)
        continue
      }
      const keys = new Set(table.map((field) => field.key))
      for (const field of group.fields) {
        if (!keys.has(field.key)) missing.push(`${group.group}.${field.key}`)
      }
    }
    expect(missing).toEqual([])
  })

  it('does not keep a row the backend no longer has', () => {
    // 反向也查：后端删了字段而页面还留着一行，保存会 422（未知字段被拒），
    // 而错误信息只会说"某个键不认识"，用户看不出是哪一行。
    const extra: string[] = []
    for (const group of schema.groups) {
      const table = tableOf(group.group)
      if (table === undefined) continue
      const keys = new Set(group.fields.map((field) => field.key))
      for (const field of table) {
        if (!keys.has(field.key)) extra.push(`${group.group}.${field.key}`)
      }
    }
    expect(extra).toEqual([])
  })

  it('keeps the numeric bounds in step with the backend', () => {
    // FIELDS 的 min/max 是**手抄**自 pydantic 的 ge/le，两份重复。抄错的症状是
    // "页面能填、保存 422"，而错误文案只说"输入不合法"——用户看不出是边界漂移。
    //
    // `Field(gt=0)` 在整数上等于"最小 1"：反射出 `minimum=0, exclusive=true`，
    // 所以这里要补一，否则把 `min: 1` 判成错误。
    const drifted: string[] = []
    for (const group of schema.groups) {
      const table = tableOf(group.group) ?? []
      for (const field of group.fields) {
        if (field.kind !== 'int' && field.kind !== 'float') continue
        const row = table.find((item) => item.key === field.key)
        if (row === undefined) continue
        // 只在**整数**上补一：`gt=0` 对 int 等于"最小 1"；对 float 是"大于 0"，
        // 而 number 输入的 `min` 表达不了严格大于，只能写 0、靠保存时报错兜住。
        const expectedMin = field.minimum === null
          ? undefined
          : field.kind === 'int' && field.minimum_exclusive
            ? Math.floor(field.minimum) + 1
            : field.minimum
        const expectedMax = field.maximum === null ? undefined : field.maximum
        const actualMax = row.max ?? undefined
        if (row.min !== expectedMin || actualMax !== expectedMax) {
          drifted.push(
            `${group.group}.${field.key}：页面 [${String(row.min)}, ${String(row.max)}] / `
            + `后端 [${String(expectedMin)}, ${String(expectedMax)}]`,
          )
        }
      }
    }
    expect(drifted).toEqual([])
  })

  it('offers exactly the choices the backend enum accepts', () => {
    // 后端加了枚举值而页面下拉没加 = 用户选不到那个值。2026-10-26 差点在
    // `web_access.provider` 上重演（新增 deepseek），所以这条单独钉住。
    const mismatched: string[] = []
    for (const group of schema.groups) {
      const table = tableOf(group.group) ?? []
      for (const field of group.fields) {
        if (field.kind !== 'choice') continue
        const row = table.find((item) => item.key === field.key)
        if (row === undefined) continue
        const offered = new Set((row.options ?? []).map(([value]) => value))
        const declared = new Set(field.options)
        const same = offered.size === declared.size && [...declared].every((value) => offered.has(value))
        if (!same) {
          mismatched.push(
            `${group.group}.${field.key}：页面 [${[...offered].join(', ')}] / 后端 [${[...declared].join(', ')}]`,
          )
        }
      }
    }
    expect(mismatched).toEqual([])
  })
})
