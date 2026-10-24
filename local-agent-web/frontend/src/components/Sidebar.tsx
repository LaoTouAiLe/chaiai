import { useState } from 'react'

import type { SessionSummaryView } from '../fixtures/types'
import { ChevronLeft, ChevronRight } from './icons'
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
   * 打开「任务中心」面板（v0.6 T62；§17.6）。
   *
   * **可选**：原型宿主（`app/App.tsx`）不传，入口就不渲染，因此原型与视觉基线的 DOM
   * 与截图一字不变。
   *
   * 入口历史：「能力」2026-10-19 并进设置（当时理由：同一件事不该两个说法）；
   * **2026-10-22 改判为独立成页**——能力开关是高频日常操作，埋在 14 个设置分区里每次都要多两步。
   * 三块能力/技能/MCP 已从设置移出，只由 {@link onOpenCapabilities} 这个入口提供。
   */
  onOpenTasks?: () => void
  /** 打开「能力中心」整页（2026-10-22 V5，规范 §5.4）。同样可选：不传则不渲染入口。 */
  onOpenCapabilities?: () => void
  /** 打开「定时任务」整页（2026-10-22 J2，契约 §21.5）。可选：不传则不渲染入口。 */
  onOpenCron?: () => void
  /** 打开「产物」整页（2026-10-22 从数据中台提出，契约 §15.11.1/§15.11.3）。 */
  onOpenArtifacts?: () => void
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
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<ReadonlySet<string>>(new Set())
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

      {/* 功能入口在顶部，对齐 hermes 的侧栏 nav 行（2026-10-22）：
          以前这五个堆在左下角，等于把最常用的跳转放在离视线最远的地方。 */}
      <nav className={styles.nav} aria-label="功能入口">
        <button type="button" className={styles.navRow} onClick={() => actions?.onNewSession()}
          aria-label="新建会话" title={collapsed ? '新建会话' : undefined}>
          <span className={styles.navIcon} aria-hidden="true">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 5v14M5 12h14" /></svg>
          </span>
          <span className={styles.navLabel}>新建会话</span>
        </button>
        {actions?.onOpenTasks ? (
          <button type="button" className={styles.navRow} onClick={actions.onOpenTasks}
            aria-label={(actions.pendingTasks ?? 0) > 0 ? `任务中心（${actions.pendingTasks} 个等你处理）` : '任务中心'}
            title={collapsed ? '任务中心' : undefined}>
            <span className={styles.navIcon} aria-hidden="true">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M4 7h16M4 12h16M4 17h9" /><circle cx="18" cy="17" r="2" /></svg>
            </span>
            <span className={styles.navLabel}>任务中心</span>
            {(actions.pendingTasks ?? 0) > 0 ? <span className={styles.navBadge} aria-hidden="true">{actions.pendingTasks}</span> : null}
          </button>
        ) : null}
        {actions?.onOpenDataCenter ? (
          <button type="button" className={styles.navRow} onClick={actions.onOpenDataCenter}
            aria-label="数据中台" title={collapsed ? '数据中台' : undefined}>
            <span className={styles.navIcon} aria-hidden="true">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M4 5h11l4 4v10H4z" /><path d="M8 11h7M8 15h5" /></svg>
            </span>
            <span className={styles.navLabel}>数据中台</span>
          </button>
        ) : null}
        {actions?.onOpenArtifacts ? (
          <button type="button" className={styles.navRow} onClick={actions.onOpenArtifacts}
            aria-label="产物" title={collapsed ? '产物' : undefined}>
            <span className={styles.navIcon} aria-hidden="true">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M12 3 3 8l9 5 9-5-9-5Z" /><path d="m3 13 9 5 9-5" /></svg>
            </span>
            <span className={styles.navLabel}>产物</span>
          </button>
        ) : null}
        {actions?.onOpenCapabilities ? (
          <button type="button" className={styles.navRow} onClick={actions.onOpenCapabilities}
            aria-label="能力中心" title={collapsed ? '能力中心' : undefined}>
            <span className={styles.navIcon} aria-hidden="true">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" /></svg>
            </span>
            <span className={styles.navLabel}>能力中心</span>
          </button>
        ) : null}
        {actions?.onOpenCron ? (
          <button type="button" className={styles.navRow} onClick={actions.onOpenCron}
            aria-label="定时任务" title={collapsed ? '定时任务' : undefined}>
            <span className={styles.navIcon} aria-hidden="true">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></svg>
            </span>
            <span className={styles.navLabel}>定时任务</span>
          </button>
        ) : null}
        {/* 设置不在这里：2026-10-22 起按 hermes 放到标题栏右上角齿轮。 */}
      </nav>

      {collapsed ? (
        <>
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
          <div className={styles.sessionScroll}>
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
                  {workspace.sessions.filter((session, index) => query !== '' || expandedWorkspaces.has(workspace.id) || index < 5 || session.selected).map((session) => (
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
                {query !== '' || !workspace.sessions.some((session, index) => index >= 5 && !session.selected) ? null : <button type="button" className={styles.showMore}
                  aria-label={`${workspace.title}${expandedWorkspaces.has(workspace.id) ? '收起会话' : '展示更多会话'}`}
                  aria-expanded={expandedWorkspaces.has(workspace.id)}
                  onClick={() => setExpandedWorkspaces((previous) => {
                    const next = new Set(previous); if (!next.delete(workspace.id)) next.add(workspace.id); return next
                  })}>{expandedWorkspaces.has(workspace.id) ? '收起' : `展示更多（${workspace.sessions.filter((session, index) => index >= 5 && !session.selected).length}）`}</button>}
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
          </div>
        </>
      )}

    </aside>
  )
}
