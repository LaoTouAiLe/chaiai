import styles from './TopBar.module.css'

interface TopBarProps {
  title: string
  /** Null while no workspace is selected: no breadcrumb is shown. */
  workspace: string | null
  activeTab: 'conversation' | 'trace'
  /**
   * 切换页签。
   *
   * 省略即原型行为：页签只显示当前态、不产生跳转（原型里"当前页签"由 `?state=` 决定）。
   */
  onSelectTab?: (tab: 'conversation' | 'trace') => void
}

/**
 * Session header: title and conversation/trace switch.
 */
export function TopBar({ title, workspace, activeTab, onSelectTab }: TopBarProps) {
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
            onClick={onSelectTab === undefined ? undefined : () => {
              onSelectTab('conversation')
            }}
          >
            对话
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'trace'}
            className={activeTab === 'trace' ? `${styles.tab} ${styles.tabActive}` : styles.tab}
            onClick={onSelectTab === undefined ? undefined : () => {
              onSelectTab('trace')
            }}
          >
            轨迹
          </button>
        </div>
      </div>
    </header>
  )
}
