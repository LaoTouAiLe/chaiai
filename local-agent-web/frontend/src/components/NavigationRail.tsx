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

/** 功能导航独立于会话目录，收起目录后所有入口仍可访问。 */
export function NavigationRail({ activePage, collapsed, pendingTasks, onNewSession, onOpenConversation, onOpenSettings, onToggleCollapse, pages }: NavigationRailProps) {
  return <div className={styles.rail}>
    <div className={styles.brand}><img src="/chaiai.svg" width="30" height="30" alt="chaiai" /></div>
    <button type="button" className={styles.compose} aria-label="新建会话" title="新建会话" onClick={onNewSession}><WorkbenchIcon name="plus" size={20} /></button>
    <nav className={styles.nav} aria-label="功能入口">
      <button type="button" className={styles.item} aria-current={activePage === null ? 'page' : undefined} aria-label="会话工作台" title="会话工作台" onClick={onOpenConversation}>
        <WorkbenchIcon name="chat" /><span>会话</span>
      </button>
      {pages.map(({ id, label, onOpen }) => <button key={id} type="button" className={styles.item} aria-current={activePage === id ? 'page' : undefined} aria-label={id === 'tasks' && pendingTasks > 0 ? `${label}（${pendingTasks} 个等你处理）` : label} title={label} onClick={onOpen}>
        <WorkbenchIcon name={id} /><span>{label === '任务中心' ? '任务' : label === '能力中心' ? '能力' : label === '定时任务' ? '定时' : label === '数据中台' ? '数据' : label}</span>
        {id === 'tasks' && pendingTasks > 0 ? <span className={styles.badge} aria-hidden="true">{pendingTasks}</span> : null}
      </button>)}
    </nav>
    <div className={styles.footer}>
      <button type="button" className={styles.utility} aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'} title={collapsed ? '展开侧边栏' : '收起侧边栏'} aria-expanded={!collapsed} onClick={onToggleCollapse}><WorkbenchIcon name={collapsed ? 'expand' : 'collapse'} /></button>
      {onOpenSettings === undefined ? null : <button type="button" className={styles.utility} aria-label="设置" title="设置" onClick={onOpenSettings}><WorkbenchIcon name="settings" /></button>}
    </div>
  </div>
}
