import type { SessionSummaryView } from './types'

export interface SidebarSessionFixture {
  id: string
  title: string
  status: SessionSummaryView['status']
  timeLabel: string
  selected: boolean
}

export interface SidebarWorkspaceFixture {
  id: string
  title: string
  sessions: readonly SidebarSessionFixture[]
  managed?: boolean
}

/**
 * Sidebar prototype data.
 *
 * Session ids are `s-<session>`, and each scenario's own session carries the
 * same title as the conversation header so the shell can never contradict
 * itself (guarded by `e2e/prototype.spec.ts`). A session may back several
 * scenarios when they are different states of the same conversation — the three
 * retrieval outcomes share `s-memory`, for example. Rows that exist only to show
 * the remaining status dots are not backed by a scenario; real session ordering
 * and counts arrive in T30.
 */
export const sidebarWorkspaces: readonly SidebarWorkspaceFixture[] = [
  {
    id: 'local-agent-web',
    title: 'local-agent-web',
    sessions: [
      { id: 's-shell', title: '给 README 补一段快速开始', status: 'completed', timeLabel: '刚刚', selected: false },
      { id: 's-new', title: '新会话', status: 'idle', timeLabel: '刚刚', selected: false },
      { id: 's-memory', title: '检索与记忆的三种结果', status: 'completed', timeLabel: '3 分钟', selected: false },
      { id: 's-diff', title: '压缩阈值重构', status: 'completed', timeLabel: '1 分钟', selected: false },
      { id: 's-errors', title: '连接故障排查', status: 'idle', timeLabel: '2 分钟', selected: false },
      // 刻意用一句长标题：侧栏那一格必须**自己省略**，而不是把右侧时间顶出去。
      // 与 `toolsScenario.session.title` 保持一致（`e2e/prototype.spec.ts` 守着这条前提）。
      {
        id: 's-tools',
        title: '定位构建失败原因：pnpm 与 vitest 两套门槛、可选依赖口径与 Windows 路径差异',
        status: 'completed',
        timeLabel: '3 分钟',
        selected: false,
      },
      { id: 's-approval', title: '清理构建产物', status: 'awaiting-approval', timeLabel: '4 分钟', selected: false },
      { id: 's-running', title: '检查可选依赖口径', status: 'running', timeLabel: '5 分钟', selected: false },
    ],
  },
  {
    id: 'chaiai',
    title: 'chaiai',
    sessions: [
      { id: 's-docs', title: '整理 v0.4 上位文档', status: 'idle', timeLabel: '昨天', selected: false },
    ],
  },
]
