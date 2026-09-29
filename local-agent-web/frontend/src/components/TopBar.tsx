import styles from './TopBar.module.css'

interface TopBarProps {
  title: string
  /** Null while no workspace is selected: no breadcrumb is shown. */
  workspace: string | null
  activeTab: 'conversation' | 'trace'
}

/**
 * Session header: title, the conversation/trace switch and the session-log entry.
 *
 * Tabs are rendered but inert — they become real views in T31.
 */
export function TopBar({ title, workspace, activeTab }: TopBarProps) {
  return (
    <header className={styles.bar}>
      <div className={styles.left}>
        {workspace === null ? null : <div className={styles.breadcrumb}>{workspace}</div>}
        <h1 className={styles.title}>{title}</h1>
      </div>
      <div className={styles.right}>
        <div className={styles.tabs} role="tablist" aria-label="会话视图">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'conversation'}
            className={activeTab === 'conversation' ? `${styles.tab} ${styles.tabActive}` : styles.tab}
          >
            对话
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'trace'}
            className={activeTab === 'trace' ? `${styles.tab} ${styles.tabActive}` : styles.tab}
          >
            轨迹
          </button>
        </div>
        <button type="button" className={styles.log}>
          Session log
        </button>
      </div>
    </header>
  )
}
