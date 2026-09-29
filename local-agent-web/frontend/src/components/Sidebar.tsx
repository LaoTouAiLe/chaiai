import type { SessionSummaryView } from '../fixtures/types'
import { ChevronRight } from './icons'
import styles from './Sidebar.module.css'

interface SidebarSession {
  id: string
  title: string
  status: SessionSummaryView['status']
  timeLabel: string
  selected: boolean
}

/**
 * Real wiring (T30). **Omitting it means prototype behaviour**: the rows still
 * render — the visual baseline needs them — but clicking does nothing. Same
 * approach as `SettingsDialog`'s `actions`: one render path, no second branch.
 */
export interface SidebarActions {
  onSelectWorkspace: (workspaceId: string) => void
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onOpenSettings: () => void
}

interface SidebarProps {
  workspaces: ReadonlyArray<{ id: string; title: string; sessions: readonly SidebarSession[] }>
  /**
   * Narrow rail (matrix 24): brand, new session, expand and settings, with no
   * session tree. Nothing is secretly selected — the tree is not rendered at all.
   */
  collapsed?: boolean
  actions?: SidebarActions
}

/**
 * Spoken form of the run state.
 *
 * The dot is decorative, so the status is carried as hidden text: a screen
 * reader must be able to tell "等待审批" from "空闲", and tests can assert the
 * state the user actually sees.
 */
const STATUS_TEXT: Record<SessionSummaryView['status'], string> = {
  idle: '空闲',
  running: '运行中',
  'awaiting-approval': '等待审批',
  completed: '已完成',
}

/** Status dot colour is the only place a run state shows up in the shell. */
function statusClass(status: SessionSummaryView['status']): string {
  switch (status) {
    case 'running':
      return styles.dotRunning ?? ''
    case 'awaiting-approval':
      return styles.dotWaiting ?? ''
    case 'completed':
      return styles.dotDone ?? ''
    default:
      return styles.dotIdle ?? ''
  }
}

/**
 * Resident session shell: brand row, search, and one workspace tree.
 *
 * Static prototype data only — selection and search become real in T30.
 */
export function Sidebar({ workspaces, collapsed = false, actions }: SidebarProps) {
  return (
    <aside
      className={collapsed ? `${styles.sidebar} ${styles.sidebarCollapsed}` : styles.sidebar}
      aria-label="会话与工作区"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      <div className={styles.brand}>
        <span className={styles.brandMark} aria-hidden="true" />
        {collapsed ? null : <span className={styles.brandName}>本地 Agent</span>}
      </div>

      {collapsed ? (
        <>
          <button
            type="button"
            className={styles.railButton}
            aria-label="新建会话"
            onClick={() => actions?.onNewSession()}
          >
            <span aria-hidden="true">＋</span>
          </button>
          <button type="button" className={styles.railButton} aria-label="展开侧边栏" aria-expanded={false}>
            <ChevronRight />
          </button>
        </>
      ) : (
        <>
          <button type="button" className={styles.newSession} onClick={() => actions?.onNewSession()}>
            <span aria-hidden="true">＋</span>
            新建会话
          </button>

          <label className={styles.search}>
            <span className="visually-hidden">搜索会话</span>
            <input placeholder="搜索会话…" type="search" />
          </label>

          <div className={styles.treeLabel}>工作区</div>
          <nav aria-label="会话">
            {workspaces.map((workspace) => (
              <div key={workspace.id} className={styles.workspace}>
                <button
                  type="button"
                  className={styles.workspaceTitle}
                  onClick={() => actions?.onSelectWorkspace(workspace.id)}
                >
                  {workspace.title}
                </button>
                <ul>
                  {workspace.sessions.map((session) => (
                    <li key={session.id} aria-current={session.selected ? 'true' : undefined}>
                      <button
                        type="button"
                        className={session.selected ? `${styles.session} ${styles.sessionSelected}` : styles.session}
                        onClick={() => actions?.onSelectSession(session.id)}
                      >
                        <span className={`${styles.dot} ${statusClass(session.status)}`} aria-hidden="true" />
                        <span className="visually-hidden">{STATUS_TEXT[session.status]}</span>
                        <span className={styles.sessionTitle}>{session.title}</span>
                        <span className={styles.sessionTime}>{session.timeLabel}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
        </>
      )}

      <div className={styles.footer}>
        <button type="button" className={styles.footerButton} onClick={() => actions?.onOpenSettings()}>
          设置
        </button>
      </div>
    </aside>
  )
}
