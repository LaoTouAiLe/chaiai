// 会话/工作区 DTO → 侧栏视图模型（T30；逐会话状态见 v0.8 T71 / §19.1）。
//
// 一条必须守住的契约：`SessionSummaryView`（传输）**不含运行状态**——列表不逐行读 Checkpoint
// （T28/T62 的口径）。所以状态来自**两个既有只读来源的合并**：
//   · 当前打开的会话：它自己的快照（更细、更即时，权威）；
//   · 其余行：**任务索引**（`GET /api/tasks`，v0.6 T62 已有），按 `sessionId` 聚合。
// 没有任务的行**不显示状态**（空闲）——不编一个状态点出来；这既是不造假，也让原型夹具
// （没有任务索引）的 DOM 与视觉基线一字不变。

import type { components } from '../../api/schema.gen'
import type { SessionSummaryView, TaskState, TasksView } from '../../fixtures/types'
import type { SidebarWorkspaceFixture } from '../../fixtures/sidebar'

type WorkspaceRef = components['schemas']['WorkspaceRef']
type SessionSummary = components['schemas']['SessionSummaryView']

/**
 * 任务索引给出的"现在在做什么"（§19.1 的闭集，逐条可对照 §17.1 的任务状态）。
 *
 * `needs_review` 在**任务**层面是终态，但它仍然"等你处理"（§17.6 的 `pending_count` 就是
 * `waiting_user + needs_review`），所以这里归到 `review`，并在措辞上与"在等审批"分开。
 */
export type SessionTaskState = 'running' | 'waiting' | 'review'

/** 任务状态 → 侧栏状态；**穷举闭集**（新增任务状态时这里会编译报错，不会静默漏掉）。 */
const SESSION_STATE_BY_TASK: Record<TaskState, SessionTaskState | null> = {
  queued: 'running',
  running: 'running',
  waiting_user: 'waiting',
  needs_review: 'review',
  completed: null,
  failed: null,
  cancelled: null,
}

/** 同一会话有多个非终态任务时取优先级最高的：**正在跑** > 在等审批 > 需人工核对。 */
const STATE_RANK: Record<SessionTaskState, number> = { running: 2, waiting: 1, review: 0 }

/** 侧栏可见措辞（§19.1：只给用户话，不给内部标识）。 */
const SESSION_STATE_LABEL: Record<SessionTaskState, string> = {
  running: '正在跑',
  waiting: '在等你处理',
  review: '需人工核对',
}

/** 任务状态映射到状态点（`SessionSummaryView['status']` 没有"需核对"这一档，与"等待审批"共用一个点）。 */
const SESSION_STATUS_BY_TASK: Record<SessionTaskState, SessionSummaryView['status']> = {
  running: 'running',
  waiting: 'awaiting-approval',
  review: 'awaiting-approval',
}

/** 任务中心的一页 → 逐会话的当前状态；没有非终态任务的会话不在结果里。 */
export function sessionTaskStates(view: TasksView | null | undefined): Map<string, SessionTaskState> {
  const result = new Map<string, SessionTaskState>()
  for (const row of view?.tasks ?? []) {
    const next = SESSION_STATE_BY_TASK[row.state]
    if (next === null) continue
    const current = result.get(row.sessionId)
    if (current === undefined || STATE_RANK[next] > STATE_RANK[current]) {
      result.set(row.sessionId, next)
    }
  }
  return result
}

export interface SidebarInputs {
  workspaces: readonly WorkspaceRef[]
  sessions: readonly SessionSummary[]
  activeSessionId: string | null
  /** 当前会话的运行状态（取自它的快照）；没有打开会话时为 `null`。 */
  activeStatus: SessionSummaryView['status'] | null
  /** 逐会话的任务状态（来自任务中心）；缺省表示"没有任务索引"，此时只有选中行有状态。 */
  sessionTasks?: ReadonlyMap<string, SessionTaskState>
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

/** 按工作区分组，标记选中行，并把任务索引的状态贴到**其余**行走（§19.1）。 */
export function mapSidebar(inputs: SidebarInputs, now: number = Date.now()): SidebarWorkspaceFixture[] {
  const { workspaces, sessions, activeSessionId, activeStatus, sessionTasks } = inputs
  const group = (items: readonly SessionSummary[]) => items.map((session) => {
    const selected = session.session_id === activeSessionId
    // 选中行以它自己的快照为准（更即时）；任务索引只服务其余行——两者不合并成一个新状态。
    const task = selected ? undefined : sessionTasks?.get(session.session_id)
    return {
      id: session.session_id, title: session.title,
      status: selected
        ? (activeStatus ?? 'idle')
        : (task === undefined ? 'idle' as const : SESSION_STATUS_BY_TASK[task]),
      stateLabel: task === undefined ? undefined : SESSION_STATE_LABEL[task],
      timeLabel: relativeTime(session.updated_at, now), selected,
    }
  })
  const result: SidebarWorkspaceFixture[] = workspaces.map((workspace) => ({
    id: workspace.workspace_id,
    title: workspace.display_name,
    sessions: group(sessions.filter((session) => session.workspace?.workspace_id === workspace.workspace_id)),
  }))
  const ids = new Set(workspaces.map((workspace) => workspace.workspace_id))
  const ungrouped = sessions.filter((session) => !ids.has(session.workspace.workspace_id))
  if (ungrouped.length > 0) result.push({
    id: 'ungrouped', title: '未分组会话', managed: false, sessions: group(ungrouped),
  })
  return result
}
