import { Button } from './ui/Button'
import { SegmentedControl, type SegmentedOption } from './ui/SegmentedControl'
import styles from './TopBar.module.css'

type ViewTab = 'conversation' | 'trace' | 'map'

interface TopBarProps {
  title: string
  /** Null while no workspace is selected: no breadcrumb is shown. */
  workspace: string | null
  activeTab: ViewTab
  /**
   * 切换页签。
   *
   * 省略即原型行为：页签只显示当前态、不产生跳转（原型里"当前页签"由 `?state=` 决定）。
   */
  onSelectTab?: (tab: 'conversation' | 'trace') => void
  onOpenMap?: () => void
  /**
   * 右上角设置入口（2026-10-22，对齐 hermes 的标题栏齿轮）。
   *
   * **可选**：原型宿主不传，齿轮就不渲染，视觉基线不受影响。
   */
  onOpenSettings?: () => void
  /** 右栏工作面板开合（§21.6）。与设置一样可选：不传就不渲染开关。 */
  panelOpen?: boolean
  onTogglePanel?: () => void
  /**
   * 当前会话是侧聊时，它的父会话标题（§21.7）。`null` = 不是侧聊，什么都不渲染。
   *
   * 放标题栏而不是侧栏行：这是"当前这个会话"的属性，不是列表项的操作；
   * 且侧栏行菜单要改 `mapSidebar` 适配器与三处类型，成本高一个量级。
   */
  sideChatParent?: string | null
  onPromoteSideChat?: () => void
}

/**
 * Session header: title and conversation/trace switch.
 *
 * 三段式（2026-10-22 V1）：标题与页签同处左段，页签紧跟标题右侧，不再贴右边缘；
 * 右段只放"管旁边这一栏"的开关（与页签语义不同，见下方注释）。
 * 左段可被长标题压缩，页签不参与压缩，所以标题再长也不会把页签挤走。
 *
 * 页签与右栏开关在 V2 改为原语（`SegmentedControl` / `Button`），不再各自写一套胶囊样式。
 */
export function TopBar({
  title,
  workspace,
  activeTab,
  onSelectTab,
  onOpenMap,
  onOpenSettings,
  panelOpen = false,
  onTogglePanel,
  sideChatParent = null,
  onPromoteSideChat,
}: TopBarProps) {
  const options: SegmentedOption<ViewTab>[] = [
    { value: 'conversation', label: '对话' },
    { value: 'trace', label: '轨迹' },
  ]
  if (onOpenMap !== undefined) options.push({ value: 'map', label: '会话地图' })

  const interactive = onSelectTab !== undefined || onOpenMap !== undefined
  const select = (tab: ViewTab) => {
    // 地图不是并列页签，是"打开另一块视图"，所以走自己的回调。
    if (tab === 'map') {
      onOpenMap?.()
      return
    }
    onSelectTab?.(tab)
  }

  return (
    <header className={styles.bar}>
      <div className={styles.lead}>
        <div className={styles.left}>
          {workspace === null ? null : <div className={styles.breadcrumb}>{workspace}</div>}
          <h1 className={styles.title}>{title}</h1>
        </div>
        <SegmentedControl
          options={options}
          value={activeTab}
          onChange={interactive ? select : undefined}
          ariaLabel="会话视图"
        />
      </div>
      {sideChatParent === null ? null : (
        <button
          type="button"
          className={styles.sideChatTag}
          onClick={onPromoteSideChat}
          title={`这是从「${sideChatParent}」开的侧聊。点一下变成正式会话。`}
        >
          侧聊
          {onPromoteSideChat === undefined ? null : <span className={styles.sideChatHint}>提升</span>}
        </button>
      )}
      {onOpenSettings === undefined && onTogglePanel === undefined ? null : (
        // 标题栏最右：设置与右栏开关都是"整个应用"的，不跟左边的页签同组。
        <div className={styles.right}>
          {onTogglePanel === undefined ? null : (
            <Button
              variant={panelOpen ? 'secondary' : 'icon'}
              size="sm"
              aria-label="工作面板"
              aria-pressed={panelOpen}
              title="工作面板"
              onClick={onTogglePanel}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
                <rect x="3" y="4" width="18" height="16" rx="2" />
                <path d="M15 4v16" />
              </svg>
            </Button>
          )}
          {onOpenSettings === undefined ? null : (
            <Button variant="icon" size="sm" aria-label="设置" title="设置" onClick={onOpenSettings}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
                <circle cx="12" cy="12" r="3" />
                <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4" />
              </svg>
            </Button>
          )}
        </div>
      )}
    </header>
  )
}
