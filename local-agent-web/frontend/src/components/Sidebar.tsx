import { useState } from 'react'

import type { SessionSummaryView } from '../fixtures/types'
import { ChevronLeft, ChevronRight, Doc } from './icons'
import styles from './Sidebar.module.css'
import { SidebarItemMenu } from './SidebarItemMenu'

export interface SidebarManagement {
  renameSession: (id: string, title: string) => Promise<void>
  archiveSession: (id: string) => Promise<void>
  renameWorkspace: (id: string, title: string) => Promise<void>
  removeWorkspace: (id: string) => Promise<void>
  restoreSession: (id: string) => Promise<void>
  archivedOpen: boolean
  onToggleArchived: () => void
  archivedSessions: readonly { id: string; title: string }[]
  archivedLoading: boolean
  archivedError: string | null
}

interface SidebarSession {
  id: string
  title: string
  status: SessionSummaryView['status']
  /**
   * "现在在做什么"（v0.8 T71；§19.1）。**可选**：原型不传，所以既有 DOM 与视觉基线不变。
   *
   * 状态点颜色区分不开"正在跑"与"等你处理"（两者共用 warn 令牌），所以这一行走**可见文字**；
   * 屏幕阅读器仍读 `STATUS_TEXT` 那份隐藏文本。
   */
  stateLabel?: string
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
  onOpenDataCenter?: () => void
  /**
   * 打开「能力与档位」面板（v0.5 T56；§16.6）。
   *
   * **可选**：不传就不渲染入口。原型宿主（`app/App.tsx`）不传，因此原型与视觉基线的 DOM、
   * 截图一字不变——新增的产品面不该悄悄改动已有基线。
   */
  onOpenCapabilities?: () => void
  /**
   * 打开「任务中心」面板（v0.6 T62；§17.6）。
   *
   * 与 `onOpenCapabilities` 同样**可选**：原型宿主不传，入口就不渲染，因此原型与视觉基线的
   * DOM 与截图一字不变。
   */
  onOpenTasks?: () => void
  /**
   * "等你处理"的任务数（在等审批 + 需要人工核对）；为 0 或未传时不显示角标。
   *
   * 角标的作用是"**需要人工核对的任务必须能被发现**"（§17.6）——重启收口后没人知道任务停在
   * 哪一步，那正是这一片要消灭的状态。
   */
  pendingTasks?: number
  /** 展开态与窄轨态共用的同一个动作：收起 / 展开是同一枚开关。 */
  onToggleCollapse: () => void
}

interface SidebarProps {
  workspaces: ReadonlyArray<{ id: string; title: string; sessions: readonly SidebarSession[]; managed?: boolean }>
  management?: SidebarManagement
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
export function Sidebar({ workspaces, collapsed = false, actions, addWorkspace, management }: SidebarProps) {
  const [search, setSearch] = useState('')
  const [restoring, setRestoring] = useState<string | null>(null)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const query = search.trim().toLocaleLowerCase()
  const visibleWorkspaces = query === '' ? workspaces : workspaces
    .map((workspace) => ({
      ...workspace,
      sessions: workspace.title.toLocaleLowerCase().includes(query)
        ? workspace.sessions
        : workspace.sessions.filter((session) => session.title.toLocaleLowerCase().includes(query)),
    }))
    .filter((workspace) => workspace.sessions.length > 0)
  return (
    <aside
      className={collapsed ? `${styles.sidebar} ${styles.sidebarCollapsed}` : styles.sidebar}
      aria-label="会话与工作区"
      data-collapsed={collapsed ? 'true' : 'false'}
    >
      <div className={styles.brand}>
        <img className={styles.brandMark} src="/chaiai.svg" width={28} height={28}
          alt={collapsed ? 'chaiai' : ''} title="chaiai" />
        {collapsed ? null : <span className={styles.brandName}>chaiai</span>}
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
            <input placeholder="搜索会话…" type="search" value={search} onChange={(event) => setSearch(event.target.value)} />
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
            {visibleWorkspaces.map((workspace) => (
              <div key={workspace.id} className={styles.workspace}>
                <div className={styles.managedRow}>
                {workspace.managed === false ? <span className={styles.workspaceTitle}>{workspace.title}</span> : <button
                  type="button"
                  className={styles.workspaceTitle}
                  onClick={() => actions?.onSelectWorkspace(workspace.id)}
                >
                  {workspace.title}
                </button>}
                {management === undefined || workspace.managed === false ? null : <SidebarItemMenu
                  title={workspace.title} kind="工作区"
                  onRename={(title) => management.renameWorkspace(workspace.id, title)}
                  onRemove={() => management.removeWorkspace(workspace.id)} />}
                </div>
                <ul>
                  {workspace.sessions.map((session) => (
                    <li key={session.id} aria-current={session.selected ? 'true' : undefined}>
                      <div className={styles.managedRow}>
                      <button
                        type="button"
                        className={session.selected ? `${styles.session} ${styles.sessionSelected}` : styles.session}
                        onClick={() => actions?.onSelectSession(session.id)}
                      >
                        <span className={`${styles.dot} ${statusClass(session.status)}`} aria-hidden="true" />
                        <span className="visually-hidden">{STATUS_TEXT[session.status]}</span>
                        <span className={styles.sessionTitle}>{session.title}</span>
                        {session.stateLabel === undefined ? null : (
                          <span className={styles.sessionState}>{session.stateLabel}</span>
                        )}
                        <span className={styles.sessionTime}>{session.timeLabel}</span>
                      </button>
                      {management === undefined ? null : <SidebarItemMenu title={session.title} kind="会话"
                        blocked={session.status === 'running' || session.status === 'awaiting-approval'}
                        onRename={(title) => management.renameSession(session.id, title)}
                        onRemove={() => management.archiveSession(session.id)} />}
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {query !== '' && visibleWorkspaces.length === 0 ? <p className={styles.noResults}>没有匹配的会话</p> : null}
          </nav>
          {management === undefined ? null : <section className={styles.archivedSection} aria-label="已归档会话">
            <button type="button" className={styles.footerButton} aria-expanded={management.archivedOpen}
              onClick={management.onToggleArchived}>已归档会话</button>
            {!management.archivedOpen ? null : <>
              <p className={styles.noResults}>归档保留消息和运行记录，可随时恢复。</p>
              {management.archivedLoading ? <p role="status">正在读取…</p> : null}
              {management.archivedError === null ? null : <p role="alert">{management.archivedError}</p>}
              {restoreError === null ? null : <p role="alert">{restoreError}</p>}
              {!management.archivedLoading && management.archivedSessions.length === 0 ? <p className={styles.noResults}>没有已归档会话</p> : null}
              {management.archivedSessions.map((item) => <div className={styles.archivedRow} key={item.id}>
                <span className={styles.sessionTitle} title={item.title}>{item.title}</span>
                <button type="button" disabled={restoring !== null} aria-label={`恢复会话「${item.title}」`}
                  onClick={async () => {
                    setRestoring(item.id); setRestoreError(null)
                    try { await management.restoreSession(item.id) }
                    catch { setRestoreError('恢复未保存，请检查连接后重试。') }
                    finally { setRestoring(null) }
                  }}>{restoring === item.id ? '恢复中…' : '恢复'}</button>
              </div>)}
            </>}
          </section>}
        </>
      )}

      <div className={styles.footer}>
        {actions?.onOpenCapabilities ? <button type="button" className={styles.footerButton} onClick={actions.onOpenCapabilities} aria-label="能力与档位" title={collapsed ? '能力与档位' : undefined}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <rect x="4" y="5" width="16" height="14" rx="2" /><path d="M8 10h8M8 14h5" />
          </svg>{collapsed ? null : <span>能力与档位</span>}
        </button> : null}
        {actions?.onOpenTasks ? <button type="button" className={styles.footerButton}
          onClick={actions.onOpenTasks}
          aria-label={(actions.pendingTasks ?? 0) > 0 ? `任务中心（${actions.pendingTasks} 个等你处理）` : '任务中心'}
          title={collapsed ? '任务中心' : undefined}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M4 7h16M4 12h16M4 17h9" /><circle cx="18" cy="17" r="2" />
          </svg>
          {collapsed ? null : <span>任务中心</span>}
          {(actions.pendingTasks ?? 0) > 0 ? (
            <span className={styles.footerBadge} aria-hidden="true">{actions.pendingTasks}</span>
          ) : null}
        </button> : null}
        {actions?.onOpenDataCenter ? <button type="button" className={styles.footerButton} onClick={actions.onOpenDataCenter} aria-label="数据中台" title={collapsed ? '数据中台' : undefined}>
          <Doc size={18} />{collapsed ? null : <span>数据中台</span>}
        </button> : null}
        <button type="button" className={styles.footerButton} onClick={() => actions?.onOpenSettings()} aria-label="设置" title={collapsed ? '设置' : undefined}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M4 6h16M4 12h16M4 18h16" /><circle cx="8" cy="6" r="2" /><circle cx="16" cy="12" r="2" /><circle cx="10" cy="18" r="2" />
          </svg>{collapsed ? null : <span>设置</span>}
        </button>
      </div>
    </aside>
  )
}
