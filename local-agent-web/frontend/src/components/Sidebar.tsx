import { useState } from 'react'

import type { SessionSummaryView } from '../fixtures/types'
import styles from './Sidebar.module.css'
import { SidebarItemMenu } from './SidebarItemMenu'
import { NavigationRail } from './NavigationRail'
import { WorkbenchIcon } from './WorkbenchIcon'

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
  onOpenConversation?: () => void
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
  /** 当前打开的功能页；仅表达导航状态，不保存或改变页面状态。 */
  activePage?: 'tasks' | 'data' | 'artifacts' | 'capabilities' | 'cron' | null
  workspaces: ReadonlyArray<{ id: string; title: string; sessions: readonly SidebarSession[]; managed?: boolean }>
  management?: SidebarManagement
  /** 只收起会话目录，独立功能导航始终保留。 */
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
  // 2026-10-28：提问挂起是"等你回答一句"，与"等你批准一个动作"分开说。
  'awaiting-input': '等待回答',
  completed: '已完成',
}

/** 颜色只作辅助，运行状态同时有可访问的文字表达。 */
function statusClass(status: SessionSummaryView['status']): string {
  switch (status) {
    case 'running':
      return styles.dotRunning ?? ''
    case 'awaiting-approval':
    case 'awaiting-input':
      return styles.dotWaiting ?? ''
    case 'completed':
      return styles.dotDone ?? ''
    default:
      return styles.dotIdle ?? ''
  }
}

/**
 * 功能导航、可收起的会话目录与工作区管理，共用真实数据和静态场景。
 */
export function Sidebar({ workspaces, collapsed = false, actions, addWorkspace, management, activePage = null }: SidebarProps) {
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<'all' | 'running' | 'attention'>('all')
  const [expandedWorkspaces, setExpandedWorkspaces] = useState<ReadonlySet<string>>(new Set())
  const [restoring, setRestoring] = useState<string | null>(null)
  const [restoreError, setRestoreError] = useState<string | null>(null)
  const query = search.trim().toLocaleLowerCase()
  const visibleWorkspaces = workspaces.map((workspace) => ({
    ...workspace,
    sessions: workspace.sessions.filter((session) => {
      const matchesQuery = query === '' || workspace.title.toLocaleLowerCase().includes(query) || session.title.toLocaleLowerCase().includes(query)
      const matchesFilter = filter === 'all' || (filter === 'running' ? session.status === 'running' : session.status === 'awaiting-approval' || session.status === 'awaiting-input')
      return matchesQuery && matchesFilter
    }),
  })).filter((workspace) => (query === '' && filter === 'all') || workspace.sessions.length > 0)
  const filtered = query !== '' || filter !== 'all'
  const sessionCount = workspaces.reduce((count, workspace) => count + workspace.sessions.length, 0)
  const pages = [
    { id: 'tasks' as const, label: '任务中心', onOpen: actions?.onOpenTasks },
    { id: 'artifacts' as const, label: '产物', onOpen: actions?.onOpenArtifacts },
    { id: 'capabilities' as const, label: '能力中心', onOpen: actions?.onOpenCapabilities },
    { id: 'cron' as const, label: '定时任务', onOpen: actions?.onOpenCron },
    { id: 'data' as const, label: '数据中台', onOpen: actions?.onOpenDataCenter },
  ].filter((page) => actions === undefined || page.onOpen !== undefined)

  return <aside className={collapsed ? `${styles.sidebar} ${styles.sidebarCollapsed}` : styles.sidebar} aria-label="会话与工作区" data-collapsed={collapsed ? 'true' : 'false'}>
    <NavigationRail activePage={activePage} collapsed={collapsed} pendingTasks={actions?.pendingTasks ?? 0}
      onNewSession={actions?.onNewSession} onOpenConversation={actions?.onOpenConversation}
      onOpenSettings={actions?.onOpenSettings} onToggleCollapse={actions?.onToggleCollapse} pages={pages} />
    {collapsed ? null : <div className={styles.navigator}>
      <header className={styles.header}>
        <div><p className={styles.eyebrow}>CHAIAI WORKSPACE</p><h2 className={styles.heading}>会话<span className={styles.count}>{sessionCount}</span></h2></div>
        <span className={styles.localBadge}><span />本地</span>
      </header>
      <label className={styles.search}>
        <span className="visually-hidden">搜索会话</span><WorkbenchIcon name="search" size={15} />
        <input placeholder="搜索会话或工作区" type="search" value={search} onChange={(event) => setSearch(event.target.value)} />
      </label>
      <div className={styles.filters} role="group" aria-label="筛选会话">
        <button type="button" aria-label="全部会话" aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>全部</button>
        <button type="button" aria-label="进行中的会话" aria-pressed={filter === 'running'} onClick={() => setFilter('running')}>进行中</button>
        <button type="button" aria-label="等待处理的会话" aria-pressed={filter === 'attention'} onClick={() => setFilter('attention')}>待处理</button>
      </div>
      <div className={styles.treeHeader}>
        <span className={styles.treeLabel}>工作区</span>
        <button type="button" className={styles.addWorkspace} aria-label="添加工作区" title="添加工作区" disabled={addWorkspace?.pending === true} onClick={() => actions?.onAddWorkspace()}><WorkbenchIcon name="plus" size={15} /></button>
      </div>
      {addWorkspace?.error == null ? null : <p className={styles.treeError} role="status">{addWorkspace.error}</p>}
      <div className={styles.sessionScroll}>
        <nav aria-label="会话">
          {visibleWorkspaces.map((workspace) => <div key={workspace.id} className={styles.workspace}>
            <div className={styles.managedRow}>
              <WorkbenchIcon name="folder" size={15} />
              {workspace.managed === false ? <span className={styles.workspaceTitle}>{workspace.title}</span> : <button type="button" className={styles.workspaceTitle} onClick={() => actions?.onSelectWorkspace(workspace.id)}>{workspace.title}</button>}
              {management === undefined || workspace.managed === false ? null : <SidebarItemMenu title={workspace.title} kind="工作区" onRename={(title) => management.renameWorkspace(workspace.id, title)} onRemove={() => management.removeWorkspace(workspace.id)} />}
            </div>
            <ul className={styles.sessions}>
              {workspace.sessions.filter((session, index) => filtered || expandedWorkspaces.has(workspace.id) || index < 5 || session.selected).map((session) => <li key={session.id} aria-current={session.selected ? 'true' : undefined}>
                <div className={styles.sessionRow} data-selected={session.selected ? 'true' : undefined}>
                  <button type="button" className={session.selected ? `${styles.session} ${styles.sessionSelected}` : styles.session} onClick={() => actions?.onSelectSession(session.id)}>
                    <span className={`${styles.dot} ${statusClass(session.status)}`} aria-hidden="true" />
                    <span className="visually-hidden">{STATUS_TEXT[session.status]}</span>
                    <span className={styles.sessionTitle}>{session.title}</span>
                    <span className={styles.sessionMeta}>
                      {session.stateLabel === undefined ? <span aria-hidden="true">{session.status === 'running' ? '正在执行' : session.status === 'awaiting-approval' ? '等你确认' : session.status === 'awaiting-input' ? '等你回答' : session.status === 'completed' ? '任务已结束' : '本地会话'}</span> : <span className={styles.sessionState}>{session.stateLabel}</span>}
                      <span className={styles.sessionTime}>{session.timeLabel}</span>
                    </span>
                  </button>
                  {management === undefined ? null : <SidebarItemMenu title={session.title} kind="会话" blocked={session.status === 'running' || session.status === 'awaiting-approval' || session.status === 'awaiting-input'} onRename={(title) => management.renameSession(session.id, title)} onRemove={() => management.archiveSession(session.id)} />}
                </div>
              </li>)}
            </ul>
            {filtered || !workspace.sessions.some((session, index) => index >= 5 && !session.selected) ? null : <button type="button" className={styles.showMore} aria-label={`${workspace.title}${expandedWorkspaces.has(workspace.id) ? '收起会话' : '展示更多会话'}`} aria-expanded={expandedWorkspaces.has(workspace.id)} onClick={() => setExpandedWorkspaces((previous) => {
              const next = new Set(previous); if (!next.delete(workspace.id)) next.add(workspace.id); return next
            })}>{expandedWorkspaces.has(workspace.id) ? '收起' : `展示更多（${workspace.sessions.filter((session, index) => index >= 5 && !session.selected).length}）`}</button>}
          </div>)}
          {filtered && visibleWorkspaces.length === 0 ? <p className={styles.noResults}>{query !== '' ? '没有匹配的会话' : '当前没有这类会话'}</p> : null}
          {!filtered && sessionCount === 0 ? <div className={styles.emptyList}><WorkbenchIcon name="chat" size={25} /><strong>工作从一次对话开始</strong><p>点击左侧 ＋ 新建会话，或添加一个工作区。</p></div> : null}
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
      <footer className={styles.navigatorFooter}><span className={styles.localDot} />会话与文件保存在本机</footer>
    </div>}
  </aside>
}
