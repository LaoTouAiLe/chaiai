// 任务中心的展示规则（v0.6 T62；契约 §17.6）。
//
// 这一层是纯函数，所以"时长如实""筛选只筛列表、计数取全量""闭集不多不少"都能在这里钉死，
// 不需要渲染、更不需要真后端。

import { describe, expect, it } from 'vitest'

import type { TaskRowView, TaskState, TasksView } from '../../fixtures/types'
import {
  TASK_FINISHED_STATES,
  TASK_PENDING_STATES,
  TASK_STATE_LABEL,
  TASK_WAITING_LABEL,
  pendingBadge,
  taskFilterCounts,
  taskRows,
  toTaskRow,
} from './adapters'

const NOW = Date.parse('2026-10-10T12:00:00Z')

/** 任务状态的完整闭集（与后端 `JOB_STATES` 同序，用于"不多不少"的断言）。 */
const ALL_STATES: readonly TaskState[] = [
  'queued',
  'running',
  'waiting_user',
  'completed',
  'failed',
  'cancelled',
  'needs_review',
]

function row(overrides: Partial<TaskRowView> = {}): TaskRowView {
  return {
    taskId: 'job-1',
    sessionId: 'session-1',
    sessionTitle: '会话',
    state: 'completed',
    detail: null,
    createdAt: '2026-10-10T11:00:00Z',
    startedAt: null,
    finishedAt: null,
    ...overrides,
  }
}

function view(tasks: TaskRowView[], pendingCount = 0): TasksView {
  const counts = Object.fromEntries(ALL_STATES.map((state) => [state, 0])) as Record<TaskState, number>
  tasks.forEach((task) => { counts[task.state] += 1 })
  return { tasks, counts, pendingCount }
}

describe('标签闭集', () => {
  it('七个状态都有中文名与"在等什么"，不多不少', () => {
    expect(Object.keys(TASK_STATE_LABEL).sort()).toEqual([...ALL_STATES].sort())
    expect(Object.keys(TASK_WAITING_LABEL).sort()).toEqual([...ALL_STATES].sort())
  })

  it('"在等什么"逐个对上契约那张表（不读会话内部状态）', () => {
    expect(TASK_WAITING_LABEL.queued).toBe('在等空闲执行位')
    expect(TASK_WAITING_LABEL.running).toBe('没有在等外部输入')
    expect(TASK_WAITING_LABEL.waiting_user).toBe('在等你的决定（审批）')
    expect(TASK_WAITING_LABEL.needs_review).toBe('已结束，等你核对')
    expect(TASK_WAITING_LABEL.completed).toBe('已结束')
  })

  it('需要你处理 = 在等审批 + 需要人工核对；终态四个', () => {
    expect([...TASK_PENDING_STATES]).toEqual(['waiting_user', 'needs_review'])
    expect([...TASK_FINISHED_STATES]).toEqual(['completed', 'failed', 'cancelled', 'needs_review'])
  })
})

describe('已运行时长如实', () => {
  it('运行中按当前时刻算，并要求页面继续刷新', () => {
    const model = toTaskRow(row({ state: 'running', startedAt: '2026-10-10T11:57:30Z' }), NOW)

    expect(model.durationText).toBe('2 分 30 秒')
    expect(model.ticking).toBe(true)
    expect(model.waitingLabel).toBe('没有在等外部输入')
  })

  it('结束的任务显示总时长，并且不再刷新', () => {
    const model = toTaskRow(
      row({ state: 'completed', startedAt: '2026-10-10T11:00:00Z', finishedAt: '2026-10-10T11:00:05Z' }),
      NOW,
    )

    expect(model.durationText).toBe('5 秒')
    expect(model.ticking).toBe(false)
    expect(model.finishedText).toBe('1 小时')
  })

  it('在等审批的任务也显示已经跑了多久，但不要求每秒刷新', () => {
    const model = toTaskRow(row({ state: 'waiting_user', startedAt: '2026-10-10T11:59:00Z' }), NOW)

    expect(model.durationText).toBe('1 分 0 秒')
    expect(model.ticking).toBe(false)
  })

  it('没有起始时间就写 null（页面显示"未记录"）——**不拿当前时间顶替**', () => {
    expect(toTaskRow(row({ state: 'queued' }), NOW).durationText).toBeNull()
    expect(toTaskRow(row({ state: 'running' }), NOW).durationText).toBeNull()
    // 结束时间有了、起始时间没有：总时长同样算不出来，如实留空
    expect(
      toTaskRow(row({ state: 'failed', finishedAt: '2026-10-10T11:00:00Z' }), NOW).durationText,
    ).toBeNull()
  })

  it('时钟回拨或时间戳在未来时不显示负数', () => {
    const model = toTaskRow(row({ state: 'running', startedAt: '2026-10-10T12:05:00Z' }), NOW)

    expect(model.durationText).toBe('0 秒')
  })
})

describe('筛选与计数', () => {
  const tasks = view(
    [
      row({ taskId: 'a', state: 'running', startedAt: '2026-10-10T11:59:00Z' }),
      row({ taskId: 'b', state: 'waiting_user' }),
      row({ taskId: 'c', state: 'needs_review' }),
      row({ taskId: 'd', state: 'completed' }),
    ],
    2,
  )

  it('筛选只影响列表，不影响计数（计数取自全量）', () => {
    expect(taskRows(tasks, 'all').map((item) => item.taskId)).toEqual(['a', 'b', 'c', 'd'])
    expect(taskRows(tasks, 'active').map((item) => item.taskId)).toEqual(['a', 'b'])
    expect(taskRows(tasks, 'pending').map((item) => item.taskId)).toEqual(['b', 'c'])
    expect(taskRows(tasks, 'finished').map((item) => item.taskId)).toEqual(['c', 'd'])

    expect(taskFilterCounts(tasks)).toEqual({ all: 4, active: 2, pending: 2, finished: 2 })
  })

  it('筛选保持服务端给的顺序（登记时间倒序）', () => {
    const ordered = view([row({ taskId: 'new' }), row({ taskId: 'old' })])

    expect(taskRows(ordered, 'all').map((item) => item.taskId)).toEqual(['new', 'old'])
  })

  it('入口角标用服务端给的"等你处理"计数，页面不自己加总', () => {
    expect(pendingBadge(tasks)).toBe(2)
    expect(pendingBadge(null)).toBe(0)
  })
})
