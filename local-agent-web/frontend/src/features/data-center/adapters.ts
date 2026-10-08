import type { components } from '../../api/schema.gen'
import { modelPurposeLabel } from '../conversation/adapters'

export type MemoryEntry = components['schemas']['MemoryEntryView']
export type UsageEntry = components['schemas']['UsageCallView']
export const kindLabels = { medium: '中期记忆', fact: '长期事实', conclusion: '任务结论' } as const
export const statusLabels: Record<string, string> = {
  active: '有效', expired: '已过期', superseded: '已替代', conflicted: '冲突待确认', deleted: '已删除', unknown: '状态未记录',
}
export const scopeLabels: Record<string, string> = { global: '全局', user: '用户', project: '项目', workspace: '工作区' }
export function countLabel(value: number | null | undefined): string {
  return value == null ? '未完整上报' : value.toLocaleString('zh-CN')
}
export function percentLabel(value: number | null | undefined): string {
  return value == null ? '未完整上报' : `${(value * 100).toFixed(1)}%`
}
export function timeLabel(value: string | null | undefined): string {
  if (!value) return '未记录'
  const time = new Date(value)
  return Number.isNaN(time.getTime()) ? '时间未记录' : time.toLocaleString('zh-CN', { hour12: false })
}
export function purposeLabel(value: string): string {
  // 与聊天调用明细使用同一用途词表。
  return modelPurposeLabel(value)
}
export function inputTokens(row: UsageEntry): number | null {
  const call = row.call
  return call.actual_uncached_input_tokens == null || call.actual_cache_read_tokens == null ? null
    : call.actual_uncached_input_tokens + call.actual_cache_read_tokens + (call.actual_cache_write_tokens ?? 0)
}
/** 日期选择器按 UTC 日筛选，右边界不包含所选日的次日。 */
export function utcRange(from: string, to: string): { since?: string; until?: string } {
  const next = to ? new Date(`${to}T00:00:00Z`) : null
  if (next) next.setUTCDate(next.getUTCDate() + 1)
  return { since: from ? `${from}T00:00:00Z` : undefined, until: next?.toISOString() }
}
