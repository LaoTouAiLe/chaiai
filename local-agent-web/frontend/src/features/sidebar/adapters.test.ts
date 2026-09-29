import { describe, expect, it } from 'vitest'

import { mapSidebar, relativeTime } from './adapters'

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
  thread_id: `thread-${id}`,
  title,
  workspace: workspace(workspaceId, workspaceId),
  is_active: isActive,
  created_at: updatedAt,
  updated_at: updatedAt,
  last_checkpoint_id: null,
})

describe('mapSidebar', () => {
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
    // 契约里会话列表不含运行状态：别的行只能显示为空闲，不能编一个状态点出来。
    expect(other).toMatchObject({ selected: false, status: 'idle' })
  })

  it('shows a workspace with no sessions instead of hiding it', () => {
    const rows = mapSidebar(
      { workspaces: [workspace('ws-9', '空工作区')], sessions: [], activeSessionId: null, activeStatus: null },
      NOW,
    )

    expect(rows).toEqual([{ id: 'ws-9', title: '空工作区', sessions: [] }])
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
