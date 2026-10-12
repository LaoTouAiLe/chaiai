// 任务中心的展示规则（v0.6 T62；契约 §17.6）。
//
// 三条硬规矩（写进契约的，不是这里的自由发挥）：
//   1. **"在等什么"是状态的函数**——页面按 `state` 推导，不读会话内部状态，因此不可能与真源不一致；
//   2. **不展示内部标识**——`taskId` 只作列表键与动作参数，页面上一个字都不出现；
//   3. **时长如实**——有 `startedAt` 才计时；索引里没有时间戳就写"未记录"，**不拿当前时间顶替**。

import type { TaskRowView, TaskState, TasksView } from '../../fixtures/types'
import { elapsedLabel } from '../conversation/adapters'
import { relativeTime } from '../sidebar/adapters'

/** 状态的中文名。**逐一对上后端的闭集**（§17.1），不在这里加第八种。 */
export const TASK_STATE_LABEL: Record<TaskState, string> = {
  queued: '排队中',
  running: '正在执行',
  waiting_user: '在等你的决定',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  needs_review: '需要人工核对',
}

/** "当前在等什么"。逐个状态对照 §17.6 的那张表，**页面不猜**。 */
export const TASK_WAITING_LABEL: Record<TaskState, string> = {
  queued: '在等空闲执行位',
  running: '没有在等外部输入',
  waiting_user: '在等你的决定（审批）',
  completed: '已结束',
  failed: '已结束',
  cancelled: '已结束',
  needs_review: '已结束，等你核对',
}

/** 终态：不再变化、也不再计时（§17.1 的终态子集）。 */
export const TASK_FINISHED_STATES: readonly TaskState[] = [
  'completed',
  'failed',
  'cancelled',
  'needs_review',
]

/** 需要你处理的状态：入口角标用（与后端 `pending_count` 同一个口径）。 */
export const TASK_PENDING_STATES: readonly TaskState[] = ['waiting_user', 'needs_review']

export type TaskFilter = 'all' | 'active' | 'pending' | 'finished'

export const TASK_FILTERS: readonly TaskFilter[] = ['all', 'active', 'pending', 'finished']

export const TASK_FILTER_LABEL: Record<TaskFilter, string> = {
  all: '全部',
  active: '在跑',
  pending: '等你处理',
  finished: '已结束',
}

/** 一条任务的展示行：派生字段都在这里算完，展示组件不认识 `Date.parse`。 */
export interface TaskRowDisplay {
  taskId: string
  sessionId: string
  sessionTitle: string
  state: TaskState
  stateLabel: string
  waitingLabel: string
  /** 已运行多久（运行中实时、等待中固定、结束时是总时长）；没有时间戳时为 `null`。 */
  durationText: string | null
  /** 结束时间的人读标签（"3 分钟前"）；还没结束时为 `null`。 */
  finishedText: string | null
  /** 服务端写好的原因（失败 / 取消 / 需核对），原样显示。 */
  detail: string | null
  /** 运行中：页面要每秒重算时长（`elapsedLabel` 的另一半是 `RunActivity` 在用）。 */
  ticking: boolean
}

function isFinished(state: TaskState): boolean {
  return TASK_FINISHED_STATES.includes(state)
}

function durationOf(row: TaskRowView, now: number): { text: string | null; ticking: boolean } {
  const started = row.startedAt === null ? NaN : Date.parse(row.startedAt)
  if (isFinished(row.state)) {
    const finished = row.finishedAt === null ? NaN : Date.parse(row.finishedAt)
    if (Number.isFinite(started) && Number.isFinite(finished)) {
      return { text: elapsedLabel(finished - started), ticking: false }
    }
    return { text: null, ticking: false }
  }
  // 排队中还没开始执行：没有起始时间，就**不写**一个假的"已运行 0 秒"。
  if (!Number.isFinite(started)) return { text: null, ticking: false }
  return { text: elapsedLabel(now - started), ticking: row.state === 'running' }
}

export function toTaskRow(row: TaskRowView, now: number = Date.now()): TaskRowDisplay {
  const duration = durationOf(row, now)
  return {
    taskId: row.taskId,
    sessionId: row.sessionId,
    sessionTitle: row.sessionTitle,
    state: row.state,
    stateLabel: TASK_STATE_LABEL[row.state],
    waitingLabel: TASK_WAITING_LABEL[row.state],
    durationText: duration.text,
    finishedText: row.finishedAt === null ? null : relativeTime(row.finishedAt, now),
    detail: row.detail,
    ticking: duration.ticking,
  }
}

function matches(state: TaskState, filter: TaskFilter): boolean {
  if (filter === 'all') return true
  if (filter === 'finished') return isFinished(state)
  if (filter === 'pending') return TASK_PENDING_STATES.includes(state)
  return !isFinished(state)
}

/** 按筛选出的展示行，**保持服务端给的顺序**（登记时间倒序）。 */
export function taskRows(
  view: TasksView,
  filter: TaskFilter = 'all',
  now: number = Date.now(),
): TaskRowDisplay[] {
  return view.tasks.filter((row) => matches(row.state, filter)).map((row) => toTaskRow(row, now))
}

/** 筛选栏上的计数：取自**全量** `counts`（不受列表截断影响，§17.6）。 */
export function taskFilterCounts(view: TasksView): Record<TaskFilter, number> {
  const counts = view.counts
  let finished = 0
  let pending = 0
  let active = 0
  for (const [state, count] of Object.entries(counts) as [TaskState, number][]) {
    if (isFinished(state)) finished += count
    else active += count
    if (TASK_PENDING_STATES.includes(state)) pending += count
  }
  return { all: active + finished, active, pending, finished }
}

/**
 * 入口角标用哪个数：**服务端给的** `pendingCount`（= 在等审批 + 需要人工核对）。
 *
 * 页面不自己从 `counts` 加总——两个口径分叉时，角标显示的数字会比面板里的行对不上，
 * 而那种不一致没人能解释清楚。
 */
export function pendingBadge(view: TasksView | null): number {
  return view === null ? 0 : view.pendingCount
}
