import { Button } from './ui/Button'
import { SegmentedControl, type SegmentedOption } from './ui/SegmentedControl'
import { WorkbenchIcon } from './WorkbenchIcon'
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
  /** 右栏工作面板开合（§21.6）。可选：不传就不渲染开关。 */
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
 * 工作区与会话标题形成两级标题，视图与工作面板组成右侧工具区。
 * 标题可缩短，窄窗口可分行，页签继续使用同一个原语。
 *
 * 页签与右栏开关在 V2 改为原语（`SegmentedControl` / `Button`），不再各自写一套胶囊样式。
 */
export function TopBar({
  title,
  workspace,
  activeTab,
  onSelectTab,
  onOpenMap,
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

  return <header className={styles.bar}>
    <div className={styles.left}>
      <div className={styles.breadcrumb}><WorkbenchIcon name="folder" size={13} /><span>{workspace ?? '本地工作台'}</span></div>
      <h1 className={styles.title}>{title}</h1>
    </div>
    <div className={styles.actions}>
      <SegmentedControl options={options} value={activeTab} onChange={interactive ? select : undefined} ariaLabel="会话视图" variant="plain" />
      {sideChatParent === null ? null : <button type="button" className={styles.sideChatTag} onClick={onPromoteSideChat} title={`这是从「${sideChatParent}」开的侧聊。点一下变成正式会话。`}>
        侧聊{onPromoteSideChat === undefined ? null : <span className={styles.sideChatHint}>提升</span>}
      </button>}
      {onTogglePanel === undefined ? null : <Button variant={panelOpen ? 'secondary' : 'ghost'} size="sm" aria-label="工作面板" aria-pressed={panelOpen} title="工作面板" onClick={onTogglePanel}>
        <WorkbenchIcon name="expand" size={16} /><span className={styles.panelLabel}>工作面板</span>
      </Button>}
    </div>
  </header>
}
