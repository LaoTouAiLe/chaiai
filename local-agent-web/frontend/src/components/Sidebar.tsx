import type { SessionSummaryView } from '../fixtures/types'
import { ChevronLeft, ChevronRight } from './icons'
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
  /**
   * 拉起宿主的原生目录选择器去**新增**一个工作区。
   *
   * 刻意只有这一条"添加"路径：系统对话框自带"新建文件夹"，所以不必再来一个"按名称创建"的
   * 入口——同一结果两套词汇是本项目明确要避免的。
   */
  onAddWorkspace: () => void
  onSelectSession: (sessionId: string) => void
  onNewSession: () => void
  onOpenSettings: () => void
  /** 展开态与窄轨态共用的同一个动作：收起 / 展开是同一枚开关。 */
  onToggleCollapse: () => void
}

interface SidebarProps {
  workspaces: ReadonlyArray<{ id: string; title: string; sessions: readonly SidebarSession[] }>
  /**
   * Narrow rail (matrix 24): brand, new session, expand and settings, with no
   * session tree. Nothing is secretly selected — the tree is not rendered at all.
   */
  collapsed?: boolean
  actions?: SidebarActions
  /**
   * 「添加工作区」的原生选择器状态。
   *
   * 进行中要禁用入口（原生对话框可以被重复点击拉起第二次），失败要**就地**说清楚并让人能
   * 再点一次——失败信息离开操作位置就没有用了。
   */
  addWorkspace?: { pending: boolean; error: string | null }
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
export function Sidebar({ workspaces, collapsed = false, actions, addWorkspace }: SidebarProps) {
  return (
    <aside
      className={collapsed ? `${styles.sidebar} ${styles.sidebarCollapsed}` : styles.sidebar}
      aria-label="会话与工作区"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      <div className={styles.brand}>
        <span className={styles.brandMark} aria-hidden="true" />
        {collapsed ? null : <span className={styles.brandName}>本地 Agent</span>}
        {collapsed ? null : (
          <button
            type="button"
            className={styles.brandToggle}
            aria-label="收起侧边栏"
            aria-expanded={true}
            onClick={() => actions?.onToggleCollapse()}
          >
            <ChevronLeft />
          </button>
        )}
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
          <button
            type="button"
            className={styles.railButton}
            aria-label="展开侧边栏"
            aria-expanded={false}
            onClick={() => actions?.onToggleCollapse()}
          >
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

          <div className={styles.treeHeader}>
            <span className={styles.treeLabel}>工作区</span>
            <button
              type="button"
              className={styles.addWorkspace}
              aria-label="添加工作区"
              title="添加工作区"
              disabled={addWorkspace?.pending === true}
              onClick={() => actions?.onAddWorkspace()}
            >
              <span aria-hidden="true">＋</span>
            </button>
          </div>
          {addWorkspace?.error == null ? null : (
            <p className={styles.treeError} role="status">{addWorkspace.error}</p>
          )}
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
