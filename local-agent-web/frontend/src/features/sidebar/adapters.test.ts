import { describe, expect, it } from 'vitest'

import type { TaskRowView, TasksView } from '../../fixtures/types'
import { mapSidebar, relativeTime, sessionTaskStates } from './adapters'

const NOW = Date.parse('2026-09-27T12:00:00Z')

const workspace = (id: string, displayName: string) => ({ workspace_id: id, display_name: displayName })

const session = (
  id: string,
  workspaceId: string,
  title: string,
  updatedAt: string,
  isActive = false,
) => ({
  session_id: id,
  mode: 'agent' as const,
  thread_id: `thread-${id}`,
  title,
  workspace: workspace(workspaceId, workspaceId),
  is_active: isActive,
  archived: false,
  created_at: updatedAt,
  updated_at: updatedAt,
  last_checkpoint_id: null,
})

describe('mapSidebar', () => {
  it('工作区移除后历史会话进入未分组，仍可选择', () => {
    const rows = mapSidebar({ workspaces: [], sessions: [session('s1', 'removed', '历史', '2026-09-27T11:00:00Z')], activeSessionId: 's1', activeStatus: 'completed' })
    expect(rows[0]).toMatchObject({ title: '未分组会话', managed: false, sessions: [{ id: 's1', selected: true }] })
  })
  it('groups sessions under their workspace', () => {
    const rows = mapSidebar(
      {
        workspaces: [workspace('ws-1', 'local-agent-web'), workspace('ws-2', 'chaiai')],
        sessions: [
          session('s-1', 'ws-1', '第一句', '2026-09-27T11:59:30Z'),
          session('s-2', 'ws-2', '另一句', '2026-09-27T11:00:00Z'),
        ],
        activeSessionId: null,
        activeStatus: null,
      },
      NOW,
    )

    expect(rows.map((row) => [row.title, row.sessions.length])).toEqual([
      ['local-agent-web', 1],
      ['chaiai', 1],
    ])
  })

  it('marks only the open session as selected, and only it carries a run state', () => {
    const rows = mapSidebar(
      {
        workspaces: [workspace('ws-1', 'local-agent-web')],
        sessions: [
          session('s-1', 'ws-1', '打开的', '2026-09-27T11:59:30Z'),
          session('s-2', 'ws-1', '别的', '2026-09-27T11:58:00Z'),
        ],
        activeSessionId: 's-1',
        activeStatus: 'running',
      },
      NOW,
    )

    const [open, other] = rows[0]?.sessions ?? []
    expect(open).toMatchObject({ selected: true, status: 'running' })
    // `SessionSummaryView` 不含运行状态（§19.1）：没有任务索引时，别的行显示为空闲，
    // 不能编一个状态点出来。有任务索引时的行为见下面"逐会话状态"那一组。
    expect(other).toMatchObject({ selected: false, status: 'idle', stateLabel: undefined })
  })

  it('shows a workspace with no sessions instead of hiding it', () => {
    const rows = mapSidebar(
      { workspaces: [workspace('ws-9', '空工作区')], sessions: [], activeSessionId: null, activeStatus: null },
      NOW,
    )

    expect(rows).toEqual([{ id: 'ws-9', title: '空工作区', sessions: [] }])
  })
})

describe('逐会话状态（v0.8 T71；§19.1）', () => {
  const taskRow = (sessionId: string, state: TaskRowView['state']): TaskRowView => ({
    taskId: `task-${sessionId}-${state}`,
    sessionId,
    sessionTitle: '会话',
    state,
    detail: null,
    createdAt: '2026-09-27T11:59:00Z',
    startedAt: null,
    finishedAt: null,
  })

  const tasksView = (rows: readonly TaskRowView[]): TasksView => ({
    tasks: rows,
    counts: {
      queued: 0, running: 0, waiting_user: 0, completed: 0,
      failed: 0, cancelled: 0, needs_review: 0,
    },
    pendingCount: 0,
  })

  const at = (id: string, title: string) => session(id, 'ws-1', title, '2026-09-27T11:59:30Z')

  it('有任务的会话给出状态与用户措辞，没有任务的不显示状态', () => {
    const rows = mapSidebar(
      {
        workspaces: [workspace('ws-1', '项目')],
        sessions: [
          at('s-run', '在跑'), at('s-wait', '等审批'), at('s-review', '要核对'), at('s-idle', '没事'),
        ],
        activeSessionId: null,
        activeStatus: null,
        sessionTasks: new Map([
          ['s-run', 'running'], ['s-wait', 'waiting'], ['s-review', 'review'],
        ]),
      },
      NOW,
    )

    expect(rows[0]?.sessions.map((row) => [row.status, row.stateLabel])).toEqual([
      ['running', '正在跑'],
      ['awaiting-approval', '在等你处理'],
      ['awaiting-approval', '需人工核对'],
      ['idle', undefined],
    ])
  })

  it('选中行以它自己的快照为准，任务索引不覆盖它', () => {
    const rows = mapSidebar(
      {
        workspaces: [workspace('ws-1', '项目')],
        sessions: [at('s-1', '打开的')],
        activeSessionId: 's-1',
        activeStatus: 'awaiting-approval',
        sessionTasks: new Map([['s-1', 'running']]),
      },
      NOW,
    )

    expect(rows[0]?.sessions[0]).toMatchObject({ status: 'awaiting-approval', stateLabel: undefined })
  })

  it('聚合非终态任务，并取优先级最高的一种（正在跑 > 在等审批 > 需人工核对）', () => {
    const states = sessionTaskStates(tasksView([
      taskRow('s-1', 'running'), taskRow('s-1', 'needs_review'),
      taskRow('s-2', 'waiting_user'), taskRow('s-2', 'needs_review'),
      taskRow('s-3', 'completed'), taskRow('s-3', 'cancelled'), taskRow('s-3', 'failed'),
      taskRow('s-4', 'needs_review'),
    ]))

    expect([...states.entries()].sort()).toEqual([
      ['s-1', 'running'], ['s-2', 'waiting'], ['s-4', 'review'],
    ])
  })

  it('没有任务数据时不显示任何状态（原型与视觉基线不变）', () => {
    const rows = mapSidebar(
      {
        workspaces: [workspace('ws-1', '项目')],
        sessions: [at('s-1', '没事')],
        activeSessionId: null,
        activeStatus: null,
        sessionTasks: sessionTaskStates(null),
      },
      NOW,
    )

    expect(rows[0]?.sessions[0]).toMatchObject({ status: 'idle', stateLabel: undefined })
  })
})

describe('relativeTime', () => {
  it('labels the recent past in coarse buckets', () => {
    expect(relativeTime('2026-09-27T11:59:40Z', NOW)).toBe('刚刚')
    expect(relativeTime('2026-09-27T11:40:00Z', NOW)).toBe('20 分钟')
    expect(relativeTime('2026-09-27T09:00:00Z', NOW)).toBe('3 小时')
    expect(relativeTime('2026-09-26T12:00:00Z', NOW)).toBe('昨天')
  })

  it('leaves an unusable timestamp empty rather than inventing one', () => {
    expect(relativeTime('not-a-date', NOW)).toBe('')
    expect(relativeTime(null, NOW)).toBe('')
  })
})
