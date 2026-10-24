// 设置项搜索的索引与打分（2026-10-23）。
//
// 两个刻意的边界：
// 1. 索引只读 `PreferenceSection` 导出的字段表，不另建一份清单——字段表是唯一真源，
//    改字段时不需要记得同步第二个地方。
// 2. 命中项**只作为跳转目标**，不在搜索结果里直接编辑。设置是"分区草稿 + 分区保存"，
//    跨分区就地编辑会把提交语义打散（模型连接、权限也不在这份表里）。

import { ADVANCED, FIELDS, TITLES } from './PreferenceSection'
import type { Field, PreferenceGroup } from './PreferenceSection'

export interface SettingsSearchHit {
  group: PreferenceGroup
  key: string
  label: string
  note: string
  sectionLabel: string
  /** 「高级配置」的项只读：能搜到、能跳过去看，但不能改。 */
  readOnly: boolean
  /**
   * 条件显示的字段（如对象存储那几项）在当前表单里可能根本不渲染。
   * 非空时表示"要先把某个字段设成某个值才看得到"，跳不过去时用它解释原因。
   */
  requirement: string | null
}

/** 取同分区某字段的中文标签；高级配置的字段只有 key → 中文名映射。 */
function fieldLabel(group: PreferenceGroup, key: string): string {
  if (group === 'advanced') return ADVANCED[key] ?? key
  return FIELDS[group].find(field => field.key === key)?.label ?? key
}

/** 把字段值翻成展示文案（下拉取标签，其它原样）。 */
function valueLabel(group: PreferenceGroup, key: string, value: string): string {
  if (group === 'advanced') return value
  const field = FIELDS[group].find(item => item.key === key)
  return field?.options?.find(([option]) => option === value)?.[1] ?? value
}

function describeRequirement(group: PreferenceGroup, field: Field): string | null {
  const parts: string[] = []

  for (const [key, value] of field.when ?? []) {
    parts.push(`需先把「${fieldLabel(group, key)}」设为「${valueLabel(group, key, value)}」`)
  }

  // `providers` 是"随某个下拉的取值显隐"的简写，控制字段固定是同分区的 `provider`。
  // 目前只有 embedding 用它（向量生成方式）；将来别处再用，这里要一起改。
  if (field.providers !== undefined && group !== 'advanced') {
    const control = FIELDS[group].find(item => item.key === 'provider')
    if (control !== undefined) {
      const values = field.providers.map(value => valueLabel(group, 'provider', value)).join(' 或 ')
      parts.push(`需先把「${control.label}」设为「${values}」`)
    }
  }

  return parts.length === 0 ? null : parts.join('；')
}

function buildIndex(): SettingsSearchHit[] {
  const hits: SettingsSearchHit[] = []

  for (const group of Object.keys(FIELDS) as Exclude<PreferenceGroup, 'advanced'>[]) {
    for (const field of FIELDS[group]) {
      hits.push({
        group,
        key: field.key,
        label: field.label,
        note: field.note,
        sectionLabel: TITLES[group],
        readOnly: false,
        requirement: describeRequirement(group, field),
      })
    }
  }

  for (const [key, label] of Object.entries(ADVANCED)) {
    hits.push({
      group: 'advanced',
      key,
      label,
      note: '只读 · 当前进程启动时加载的配置，页面不提供修改',
      sectionLabel: TITLES.advanced,
      readOnly: true,
      requirement: null,
    })
  }

  return hits
}

/** 静态字段表的展开结果；模块加载时构建一次即可。 */
export const SETTINGS_SEARCH_INDEX: readonly SettingsSearchHit[] = buildIndex()

function normalize(text: string): string {
  return text.toLowerCase().replace(/\s+/g, ' ').trim()
}

/**
 * 打分口径照 hermes 的设置搜索：标签完全匹配 > 前缀 > 包含 > 分区命中 > 多词分散命中。
 * 同分按索引顺序，结果不会因为一次无关的重渲染而跳位。
 */
function score(hit: SettingsSearchHit, query: string): number {
  const needle = normalize(query)
  if (needle === '') return 0

  const label = normalize(hit.label)
  const section = normalize(hit.sectionLabel)
  const haystack = normalize([hit.label, hit.sectionLabel, hit.note, hit.key, hit.group].join(' '))
  const terms = needle.split(' ').filter(Boolean)

  if (!terms.every(term => haystack.includes(term))) return 0
  if (label === needle) return 100
  if (label.startsWith(needle)) return 90
  if (label.includes(needle)) return 80
  if (section.includes(needle)) return 70
  if (terms.every(term => label.includes(term) || section.includes(term))) return 60
  return 50
}

export function filterSettings(
  index: readonly SettingsSearchHit[],
  query: string,
): SettingsSearchHit[] {
  return index
    .map((hit, order) => ({ hit, order, score: score(hit, query) }))
    .filter(result => result.score > 0)
    .sort((a, b) => b.score - a.score || a.order - b.order)
    .map(result => result.hit)
}

/** 目标字段的 DOM 锚点。带分区前缀：`enabled` / `max_results` 在多个分区里重名。 */
export const settingElementId = (group: PreferenceGroup, key: string): string =>
  `field-${group}-${key}`
