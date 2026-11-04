import { WorkbenchIcon } from './WorkbenchIcon'
import styles from './NavigationRail.module.css'

export type WorkbenchPage = 'tasks' | 'data' | 'artifacts' | 'capabilities' | 'cron'

interface NavigationRailProps {
  activePage: WorkbenchPage | null
  collapsed: boolean
  pendingTasks: number
  onNewSession?: () => void
  onOpenConversation?: () => void
  onOpenSettings?: () => void
  onToggleCollapse?: () => void
  pages: { id: WorkbenchPage; label: string; onOpen?: () => void }[]
}

/** App actions live in a narrow rail, independent of the conversation list. */
export function NavigationRail({ activePage, collapsed, pendingTasks, onNewSession, onOpenConversation, onOpenSettings, onToggleCollapse, pages }: NavigationRailProps) {
  return <div className={styles.rail} data-collapsed={collapsed}>
    <div className={styles.brand}><img src="/chaiai.svg" width="26" height="26" alt="chaiai" /></div>
    {collapsed ? <>
      <button type="button" className={styles.utility} data-sidebar-toggle aria-label="展开侧边栏" title="展开侧边栏" aria-expanded={false} onClick={onToggleCollapse}><WorkbenchIcon name="expand" size={18} /></button>
      <button type="button" className={styles.utility} aria-label="新建会话" title="新建会话" onClick={onNewSession}><WorkbenchIcon name="plus" size={18} /></button>
    </> : null}
    <nav className={styles.nav} aria-label="功能入口">
      <button type="button" className={styles.item} aria-current={activePage === null ? 'page' : undefined} aria-label="会话工作台" title="会话工作台" onClick={onOpenConversation}>
        <WorkbenchIcon name="chat" size={19} />
      </button>
      {pages.map(({ id, label, onOpen }) => <button key={id} type="button" className={styles.item} aria-current={activePage === id ? 'page' : undefined} aria-label={id === 'tasks' && pendingTasks > 0 ? `${label}（${pendingTasks} 个等你处理）` : label} title={label} onClick={onOpen}>
        <WorkbenchIcon name={id} size={19} />
        {id === 'tasks' && pendingTasks > 0 ? <span className={styles.badge} aria-hidden="true">{pendingTasks}</span> : null}
      </button>)}
    </nav>
    <div className={styles.footer}>
      {onOpenSettings === undefined ? null : <button type="button" className={styles.utility} aria-label="设置" title="设置" onClick={onOpenSettings}><WorkbenchIcon name="settings" size={19} /></button>}
    </div>
  </div>
}
