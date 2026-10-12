// 「任务中心」面板（v0.6 T62；契约 §17.6）。
//
// 面板是**展示组件**（不吃 fetch），所以这里能逐条钉住三件写进契约的事：
//   1. 状态、已运行时长与"在等什么"都显示出来（验收 4）；
//   2. **不出现内部标识**（`taskId` 只作列表键与动作参数，页面上一个字都不显示，§15.8）；
//   3. 恢复入口的文案与 §17.5 的三类判定一致（不自动重跑、取消不撤销）。

import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskRowView, TaskState, TasksView } from '../../fixtures/types'
import { TasksPanel } from './TasksPanel'

const NOW = new Date('2026-10-10T12:00:00Z')

function row(overrides: Partial<TaskRowView> = {}): TaskRowView {
  return {
    taskId: 'job-secret-id',
    sessionId: 'session-1',
    sessionTitle: '跑很久的会话',
    state: 'running',
    detail: null,
    createdAt: '2026-10-10T11:00:00Z',
    startedAt: '2026-10-10T11:57:30Z',
    finishedAt: null,
    ...overrides,
  }
}

function view(tasks: TaskRowView[], pendingCount = 0): TasksView {
  const states: readonly TaskState[] = [
    'queued',
    'running',
    'waiting_user',
    'completed',
    'failed',
    'cancelled',
    'needs_review',
  ]
  const counts = Object.fromEntries(states.map((state) => [state, 0])) as Record<TaskState, number>
  tasks.forEach((task) => { counts[task.state] += 1 })
  return { tasks, counts, pendingCount }
}

function firstRow(): HTMLElement {
  return screen.getAllByRole('listitem')[0]
}

describe('任务中心面板', () => {
  // 只冻结 `Date`：时长断言要确定，"输入事件"仍走真实计时器（fake 掉 setInterval 会让 userEvent 卡住）。
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('显示状态、已运行时长与"在等什么"', () => {
    render(<TasksPanel view={view([row()])} onOpenSession={vi.fn()} />)

    expect(screen.getByText('正在执行')).toBeInTheDocument()
    expect(firstRow().textContent).toContain('已运行 2 分 30 秒')
    expect(firstRow().textContent).toContain('没有在等外部输入')
    expect(screen.getByText('跑很久的会话')).toBeInTheDocument()
  })

  it('页面上不出现内部标识（任务 id 只作列表键）', () => {
    render(<TasksPanel view={view([row()])} onOpenSession={vi.fn()} />)

    expect(document.body.textContent).not.toContain('job-secret-id')
    expect(document.body.textContent).not.toContain('session-1')
  })

  it('运行中的任务带展示时钟：每秒重新计算，不用请求服务端', () => {
    // 这一条要连计时器一起冻结：它验证的正是"页面自己在动"，不轮询服务端。
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(NOW)
    render(<TasksPanel view={view([row()])} onOpenSession={vi.fn()} />)

    expect(firstRow().textContent).toContain('2 分 30 秒')
    act(() => { vi.advanceTimersByTime(5_000) })
    expect(firstRow().textContent).toContain('2 分 35 秒')
  })

  it('取消过 / 需要核对的任务把后果写在行里（恢复入口不许暗示"撤销"或"自动重跑"）', () => {
    render(
      <TasksPanel
        view={view([
          row({ taskId: 'a', state: 'cancelled', detail: '用户取消' }),
          row({ taskId: 'b', state: 'needs_review', detail: '可能已部分执行（已写出的内容不会回滚）、可能仍有残留进程。' }),
        ])}
        onOpenSession={vi.fn()}
      />,
    )

    const text = document.body.textContent ?? ''
    expect(text).toContain('取消不撤销已经发生的副作用')
    expect(text).toContain('不自动重跑：核对后重新提交就是新任务')
    // 服务端写的原因原样显示（页面不替它编一句）
    expect(text).toContain('可能仍有残留进程')
  })

  it('"打开会话"把会话标识交给调用方，交给它去会话里处理', async () => {
    const user = userEvent.setup()
    const onOpenSession = vi.fn()
    render(
      <TasksPanel
        view={view([row({ state: 'waiting_user', startedAt: '2026-10-10T11:59:00Z' })])}
        onOpenSession={onOpenSession}
      />,
    )

    await user.click(screen.getByRole('button', { name: '打开会话做决定' }))

    expect(onOpenSession).toHaveBeenCalledWith('session-1')
  })

  it('筛选只改列表：点"已结束"后正在跑的行不再显示，计数仍来自全量', async () => {
    const user = userEvent.setup()
    render(
      <TasksPanel
        view={view([row({ taskId: 'a' }), row({ taskId: 'b', state: 'completed', finishedAt: '2026-10-10T11:59:00Z' })])}
        onOpenSession={vi.fn()}
      />,
    )

    await user.click(screen.getByRole('button', { name: /已结束/ }))

    expect(screen.queryByText('正在执行')).toBeNull()
    expect(screen.getByText('已完成')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /在跑/ }).textContent).toContain('1')
  })

  it('还没读到 / 读失败：如实说清楚，而不是显示一个空的"没有任务"', () => {
    render(<TasksPanel view={null} errorText="请求失败（HTTP 500）" onOpenSession={vi.fn()} />)

    expect(screen.getByRole('alert').textContent).toBe('请求失败（HTTP 500）')
    expect(screen.getByText('还没有读到任务——读到之前不显示任何一项。')).toBeInTheDocument()
  })

  it('确认没有任务与"筛选后为空"是两句不同的话', async () => {
    const user = userEvent.setup()
    render(<TasksPanel view={view([row()])} onOpenSession={vi.fn()} />)

    await user.click(screen.getByRole('button', { name: /已结束/ }))

    expect(screen.getByText('当前筛选下没有任务。')).toBeInTheDocument()
    expect(screen.queryByText(/这个工作区还没有任务/)).toBeNull()
  })
})
