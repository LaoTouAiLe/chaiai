// 会话/工作区 DTO → 侧栏视图模型（T30）。
//
// 一条必须守住的契约：`SessionSummaryView`（传输）**不含运行状态**——列表不伪造 running/awaiting，
// 因为逐行读所有线程的 Checkpoint 不是列表该做的事（T28 的口径）。所以除了"当前打开的那个会话"
// （它的状态来自自己的快照，是权威的），其余行一律显示为空闲，而不是编一个状态点出来。

import type { components } from '../../api/schema.gen'
import type { SessionSummaryView } from '../../fixtures/types'
import type { SidebarWorkspaceFixture } from '../../fixtures/sidebar'

type WorkspaceRef = components['schemas']['WorkspaceRef']
type SessionSummary = components['schemas']['SessionSummaryView']

export interface SidebarInputs {
  workspaces: readonly WorkspaceRef[]
  sessions: readonly SessionSummary[]
  activeSessionId: string | null
  /** 当前会话的运行状态（取自它的快照）；没有打开会话时为 `null`。 */
  activeStatus: SessionSummaryView['status'] | null
}

/**
 * 相对时间标签。`now` 可注入，测试才能确定性地断言（界面按当前时刻算）。
 *
 * 只给到"刚过去多久"这个粒度：会话列表要的是"哪个更近"，不是精确时间戳。
 */
export function relativeTime(value: string | null | undefined, now: number = Date.now()): string {
  if (value === null || value === undefined || value === '') return ''
  const at = new Date(value).getTime()
  if (Number.isNaN(at)) return ''
  const seconds = Math.max(0, Math.round((now - at) / 1000))
  if (seconds < 60) return '刚刚'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时`
  const days = Math.round(hours / 24)
  if (days === 1) return '昨天'
  if (days < 30) return `${days} 天`
  return new Date(at).toLocaleDateString()
}

/** 按工作区分组，并把当前会话标记成选中行。 */
export function mapSidebar(inputs: SidebarInputs, now: number = Date.now()): SidebarWorkspaceFixture[] {
  const { workspaces, sessions, activeSessionId, activeStatus } = inputs
  return workspaces.map((workspace) => ({
    id: workspace.workspace_id,
    title: workspace.display_name,
    sessions: sessions
      .filter((session) => session.workspace?.workspace_id === workspace.workspace_id)
      .map((session) => {
        const selected = session.session_id === activeSessionId
        return {
          id: session.session_id,
          title: session.title,
          // 只有选中的那一行有权威状态；其余行按契约"列表不含运行状态"显示为空闲。
          status: selected ? (activeStatus ?? 'idle') : 'idle',
          timeLabel: relativeTime(session.updated_at, now),
          selected,
        }
      }),
  }))
}
