import { lazy, Suspense, useEffect, useRef, useState } from 'react'

import { useArtifactsDetail, useSearchWorkspaceFiles, useSessionChanges, useSideChatCreation, useWorkspaceFiles } from '../../api/queries'
import { ApiError, closeBrowser, newRequestId, requestTerminal, stopTerminal } from '../../api/client'
import { sameFilePath } from '../conversation/fileLinks'
import type { TraceView as TraceViewModel } from '../../fixtures/types'
import { Button } from '../../components/ui/Button'
import { SearchField } from '../../components/ui/SearchField'
import { FileDiff } from '../code-changes/ChangesPanel'
import { FileIcon } from './fileIcon'
import { languageOf, useHighlight } from './useHighlight'
import { FileTree } from './FileTree'
import { SideChatPane } from './SideChatPane'
import { usePaneResize } from './usePaneResize'
import styles from './PanelShell.module.css'

const TerminalPane = lazy(() => import('./TerminalPane'))
const BrowserPane = lazy(() => import('./BrowserPane'))

const DEFAULT_WIDTH = 520
const MIN_WIDTH = 320
const MAX_WIDTH = 760

function panelScrollBehavior(): ScrollBehavior {
  return document.documentElement.dataset.motion === 'reduced'
    || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
}

type Tab = 'files' | 'changes' | 'steps' | 'sidechat' | 'terminal' | 'browser'

/** `C:\Windows\System32\cmd.exe` → `cmd`：标签上显示的名字（2026-10-28）。 */
function shellLabel(shell: string): string {
  const name = shell.split(/[\\/]/).pop() ?? shell
  return name.replace(/\.exe$/i, '').toLowerCase()
}

/**
 * 右栏首屏菜单（2026-10-23 按参考图重做）。
 *
 * 之前一打开就落在"文件"，用户看不出这一栏还能干什么；现在先列能力：
 * 图标 + 名称 + 一句说明，点谁进谁。顶部留「+」回到这里。
 */
const MENU: { id: Tab; label: string; hint: string; icon: string }[] = [
  { id: 'files', label: '文件', hint: '浏览和预览任务文件', icon: 'M4 5h11l4 4v10H4z' },
  { id: 'changes', label: '改动', hint: '本次会话改过的文件与前后对比', icon: 'M4 7h16M4 12h10M4 17h7' },
  { id: 'steps', label: '步骤', hint: '查看这次会话执行到哪一步', icon: 'M5 7h14M5 12h14M5 17h9' },
  // 侧边对话是**右栏里的一块面板**：主对话不动、不切走。
  // 之前做成"新建会话然后切过去"，用户的原话是「完全就不对」。
  { id: 'sidechat', label: '侧边对话', hint: '基于当前上下文开启独立对话，不打断当前任务', icon: 'M4 5h16v11H9l-5 4z' },
  // 终端：点一次开一个标签、一个终端（2026-10-28，不再逐次审批）。
  { id: 'terminal', label: '终端', hint: '打开一个命令行，可多开标签', icon: 'M4 6h16v12H4zM8 10l2 2-2 2' },
  { id: 'browser', label: '浏览器', hint: '浏览外网与预览本地项目', icon: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18' },
]

/**
 * 右栏工作面板（§21.6，2026-10-22）。
 *
 * 文件、改动、步骤三个标签只读；新增用户终端按 §23 单独审批。
 * 数据按当前会话取数（2026-10-22 用户反馈：「工作区的太大了」）：
 * 改动看本次会话改过哪些文件，任务看本次会话派出过哪些；文件标签的起点仍是工作区根，
 * 因为"看文件"本来就是看整个项目的。
 */
interface PanelTab {
  /** 页签唯一键；同类可以多开，所以不能用 kind 当键。 */
  id: string
  kind: Tab
  /**
   * 这页自己的参数：文件页 = 当前打开的文件路径；侧聊页 = 侧聊会话 id。
   * 存在这里而不是子组件内部，切换页签回来时才不会丢。
   */
  param: string | null
  title?: string
  browserKey?: string
}

/**
 * 面板状态落盘的位置（2026-10-23）。
 *
 * **按会话存**：右栏的内容（文件树、改动、侧聊）都属于「当前这个会话」，
 * 全局存一份会让 A 会话打开的文件在切到 B 之后还挂着，而那个路径在 B 的工作区里
 * 根本不存在。键里带会话 id，切换会话就是换一份状态。
 */
const STORAGE_PREFIX = 'panel.state.v1'
const STORAGE_VERSION = 1

function storageKey(sessionId: string | null): string {
  return `${STORAGE_PREFIX}:${sessionId ?? 'none'}`
}

interface PersistedPanel {
  tabs: PanelTab[]
  activeId: string | null
  width: number
}

/**
 * 从 localStorage 恢复面板状态。
 *
 * **只做结构校验，不做存在性校验**：判断"这个侧聊会话还在不在""这个文件还在不在"
 * 需要发请求，在渲染期做既慢又会闪。不存在的指针交给下游自然报错
 * （文件读不到、侧聊空态），用户关掉那一页即可——比把整份标签清空温和。
 */
function loadPanelState(sessionId: string | null): PersistedPanel | null {
  try {
    const raw = localStorage.getItem(storageKey(sessionId))
    if (raw === null) return null
    const parsed = JSON.parse(raw) as Partial<PersistedPanel> & { version?: number }
    if (parsed.version !== STORAGE_VERSION || !Array.isArray(parsed.tabs)) return null
    const tabs = parsed.tabs
      .filter((item): item is PanelTab =>
        typeof item?.id === 'string' && MENU.some((entry) => entry.id === item.kind))
      .map((item) => ({ id: item.id, kind: item.kind, param: typeof item.param === 'string' ? item.param : null,
        title: typeof item.title === 'string' ? item.title.slice(0, 200) : undefined,
        browserKey: item.kind === 'browser' ? typeof item.browserKey === 'string' && /^[a-zA-Z0-9_-]{1,128}$/.test(item.browserKey)
          ? item.browserKey : newRequestId() : undefined }))
    const activeId = parsed.activeId === null ? null : tabs.some((item) => item.id === parsed.activeId)
      ? (parsed.activeId ?? null)
      : (tabs[tabs.length - 1]?.id ?? null)
    const width = typeof parsed.width === 'number' && Number.isFinite(parsed.width)
      ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, parsed.width))
      : DEFAULT_WIDTH
    return { tabs, activeId, width }
  } catch {
    // 存储被写坏（手工改过、旧版本残留）就当没有，不能让整页打不开。
    return null
  }
}

export function PanelShell({
  open = true,
  workspaceId,
  sessionId,
  trace,
  onOpenStep,
  onClose,
  focus,
}: {
  open?: boolean
  workspaceId: string | null
  /** 当前会话：改动与任务两个标签都靠它收窄。 */
  sessionId: string | null
  /** 轨迹模型：步骤标签直接按它渲染，不再另取一份任务索引。 */
  trace: TraceViewModel
  /** 点一步：滚到对话里对应的那一轮（与「会话地图」的定位走同一条路径）。 */
  onOpenStep?: (turnId: string) => void
  onClose: () => void
  /** 从对话流改动条点进来时，指定要看哪个文件。 */
  focus?: { tab: Tab; path: string } | null
}) {
  // 多标签（2026-10-23）：`tabs` 是打开着的页，`activeId === null` 表示在首屏菜单。
  // 同类可多开（两个文件页、两个侧聊页都行）——键用自增 id，不用 kind。
  // 恢复上次的页签（2026-10-23）：关掉重开还能看到之前开着的东西。
  const [restored] = useState(() => loadPanelState(sessionId))
  const [tabs, setTabs] = useState<PanelTab[]>(restored?.tabs ?? [])
  const [activeId, setActiveId] = useState<string | null>(restored?.activeId ?? null)
  const [slideDirection, setSlideDirection] = useState<'left' | 'right'>('right')
  const [cleanupError, setCleanupError] = useState<string | null>(null)
  // 终端启动失败按标签记（2026-10-28）：标签立刻出现，请求在后台跑，失败原因挂回它自己。
  const [terminalIssues, setTerminalIssues] = useState<Record<string, string>>({})
  const selectTab = (id: string | null) => {
    if (id === activeId) return
    const previous = tabs.findIndex((item) => item.id === activeId)
    const next = id === null ? -1 : tabs.findIndex((item) => item.id === id)
    setSlideDirection(id === null || (next >= 0 && next < previous) ? 'left' : 'right')
    setActiveId(id)
  }
  // 未发送草稿只留在当前页面内存中，切换标签或收起面板不会丢失。
  const [sideDrafts, setSideDrafts] = useState<Record<string, string>>({})
  const { create: createSideChat, forget: forgetSideChat } = useSideChatCreation(sessionId)
  useEffect(() => {
    for (const tab of tabs) {
      if (tab.kind === 'sidechat' && tab.param !== null) forgetSideChat(tab.id)
    }
  }, [tabs, forgetSideChat])
  // 序号要接着恢复出来的最大值往后走：从 0 重新数会和已存在的 `files-1` 撞 id，
  // 撞了之后 React 的 key 重复、切页签会切到错的那页。
  const counter = useRef(restored === null ? 0 : Math.max(0,
    ...restored.tabs.map((item) => {
      const matched = /-(\d+)$/.exec(item.id)
      return matched === null ? 0 : Number(matched[1])
    })))
  const active = tabs.find((item) => item.id === activeId) ?? null

  const openTab = (kind: Tab, param?: string) => {
    counter.current += 1
    const id = `${kind}-${String(counter.current)}`
    setTabs((previous) => [...previous, { id, kind, param: param ?? null, browserKey: kind === 'browser' ? newRequestId() : undefined }])
    selectTab(id)
    // 终端：打开标签即启动一个（2026-10-28）。不再出审批卡，也不再由面板内部管理多个终端。
    if (kind === 'terminal') void startTerminal(id)
    return id
  }

  const patchTab = (id: string, param: string | null) => {
    setTabs((previous) => previous.map((item) => (item.id === id ? { ...item, param } : item)))
  }

  /**
   * 一个标签一个终端：请求成功就把 `terminal_id` 绑到标签上，失败原因留在这个标签里。
   * 标签先渲染（"正在启动…"）再回填，避免点一下要等一次网络才出现东西。
   */
  async function startTerminal(tabId: string): Promise<void> {
    if (!workspaceId) {
      setTerminalIssues((previous) => ({ ...previous, [tabId]: '先选择一个工作区。' }))
      return
    }
    try {
      const view = await requestTerminal(workspaceId)
      const terminalId = view.terminal_id
      if (terminalId == null) {
        setTerminalIssues((previous) => ({ ...previous, [tabId]: view.detail ?? '终端没有启动。' }))
        return
      }
      // 标签直接改成 shell 名（`cmd`）：多开时比"终端 2/3"更好认，也和自己开的终端一致。
      setTabs((previous) => previous.map((item) => item.id === tabId
        ? { ...item, param: terminalId, title: shellLabel(view.shell) }
        : item))
    } catch (cause) {
      setTerminalIssues((previous) => ({
        ...previous,
        [tabId]: cause instanceof Error ? cause.message : '终端启动失败。',
      }))
    }
  }

  const panelRef = useRef<HTMLElement>(null)
  const homeRef = useRef<HTMLButtonElement>(null)
  const tabRefs = useRef(new Map<string, HTMLButtonElement>())
  const tabStripRef = useRef<HTMLDivElement>(null)
  const [tabScroll, setTabScroll] = useState({ overflow: false, left: false, right: false })
  const returnFocus = useRef<HTMLElement | null>(null)
  const [present, setPresent] = useState(open)
  const requestClose = () => { returnFocus.current?.focus(); onClose() }
  const tabLabel = (item: PanelTab) => item.kind === 'files' && item.param !== null
    ? item.param.split(/[\\/]/).pop() ?? '文件'
    : item.kind === 'browser' && item.title ? item.title
    // 终端标签用 shell 名（cmd / powershell），与用户自己开的终端同名（2026-10-28）；
    // 拿不到名字时退回自增编号，避免出现空标签。
    : item.kind === 'terminal' ? (item.title ?? `终端 ${/-(\d+)$/.exec(item.id)?.[1] ?? ''}`.trim())
    : MENU.find((entry) => entry.id === item.kind)?.label ?? item.kind

  const closeTab = (id: string) => {
    const closing = tabs.find(item => item.id === id)
    if (closing?.kind === 'sidechat') forgetSideChat(id)
    if (closing?.kind === 'browser' && closing.browserKey) {
      void closeBrowser(closing.browserKey).catch(() => setCleanupError('浏览器关闭请求未成功，将在断开连接并空闲 30 分钟后结束。'))
    }
    // 终端没有「结束」按钮了：关闭标签就是结束它，否则进程只能等 30 分钟空闲回收。
    if (closing?.kind === 'terminal' && closing.param !== null) {
      void stopTerminal(closing.param).catch(() => setCleanupError('终端结束请求未成功，它会在 30 分钟无输入后自动结束。'))
    }
    setTerminalIssues((previous) => {
      if (!(id in previous)) return previous
      const remaining = { ...previous }
      delete remaining[id]
      return remaining
    })
    const index = tabs.findIndex((item) => item.id === id)
    const next = tabs.filter((item) => item.id !== id)
    const nextId = activeId === id ? (next[Math.min(index, next.length - 1)]?.id ?? null) : activeId
    setTabs(next)
    setSideDrafts((previous) => {
      const remaining = { ...previous }
      delete remaining[id]
      return remaining
    })
    selectTab(nextId)
    if (nextId === null) homeRef.current?.focus()
    else tabRefs.current.get(nextId)?.focus()
  }

  // 宽度可拖（2026-10-23）：固定宽度在宽屏上浪费、在窄屏上挤，交给用户自己拉。
  const { width, resizing, handle } = usePaneResize(restored?.width ?? DEFAULT_WIDTH, MIN_WIDTH, MAX_WIDTH, DEFAULT_WIDTH)

  // 保留退场时间；关闭后卸载内容，停止子面板轮询，但保留会话内的页签状态。
  useEffect(() => {
    if (open) {
      returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
      setPresent(true)
      return
    }
    const timer = window.setTimeout(() => {
      setPresent(false)
      if (document.activeElement === document.body || panelRef.current?.contains(document.activeElement)) returnFocus.current?.focus()
    }, document.documentElement.dataset.motion === 'reduced' ? 0 : 200)
    return () => window.clearTimeout(timer)
  }, [open])

  useEffect(() => {
    if (!open || !present) return
    const tab = activeId === null ? homeRef.current : tabRefs.current.get(activeId)
    tab?.parentElement?.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: panelScrollBehavior() })
  }, [open, present, activeId, tabScroll.overflow])

  useEffect(() => {
    if (!open || !present) return
    const tab = activeId === null ? homeRef.current : tabRefs.current.get(activeId)
    // 仅在打开面板或切换页签时移动焦点，不在文件内容刷新时抢走输入焦点。
    tab?.focus({ preventScroll: true })
  }, [open, present, activeId])

  useEffect(() => {
    const strip = tabStripRef.current
    if (strip === null) return
    const update = () => {
      const next = {
        overflow: strip.scrollWidth > (strip.parentElement?.clientWidth ?? strip.clientWidth) + 1,
        left: strip.scrollLeft > 1,
        right: strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 1,
      }
      setTabScroll(previous => previous.overflow === next.overflow && previous.left === next.left && previous.right === next.right ? previous : next)
    }
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey || event.metaKey || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return
      const limit = strip.scrollWidth - strip.clientWidth
      if (limit <= 0) return
      // 纵向滚轮用于横移；触控板原生横向滚动和缩放仍交给浏览器。
      event.preventDefault()
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16
        : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? strip.clientWidth : 1
      strip.scrollLeft = Math.max(0, Math.min(limit, strip.scrollLeft + event.deltaY * unit))
    }
    update()
    strip.addEventListener('scroll', update, { passive: true })
    strip.addEventListener('wheel', onWheel, { passive: false })
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update)
    observer?.observe(strip)
    // 文件名变化也会改变标签宽度。
    for (const child of strip.children) observer?.observe(child)
    return () => {
      strip.removeEventListener('scroll', update)
      strip.removeEventListener('wheel', onWheel)
      observer?.disconnect()
    }
  }, [tabs, present])

  const scrollTabs = (direction: number) => {
    const strip = tabStripRef.current
    strip?.scrollBy({ left: direction * Math.max(120, strip.clientWidth * .7), behavior: panelScrollBehavior() })
  }

  // 拖动时只更新画面，结束后再写存储，避免每帧同步写 localStorage。
  useEffect(() => {
    if (resizing) return
    try {
      localStorage.setItem(storageKey(sessionId), JSON.stringify({
        version: STORAGE_VERSION, tabs, activeId, width,
      }))
    } catch {
      // 存储写满或被禁用：功能照用，只是记不住。不该因此报错打断用户。
    }
  }, [tabs, activeId, width, sessionId, resizing])

  // 从对话流的改动条点进来：已有改动页就切过去并定位文件，没有就开一个。
  useEffect(() => {
    if (!focus) return
    const existing = tabs.find((item) => item.kind === focus.tab
      && (focus.tab !== 'files' || sameFilePath(item.param, focus.path)))
    if (existing !== undefined) {
      selectTab(existing.id)
      patchTab(existing.id, focus.path)
      return
    }
    openTab(focus.tab, focus.path)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只在 focus 变化时触发
  }, [focus])

  if (!present) return null

  return (
    <aside
      ref={panelRef}
      className={`${styles.panel} ${open ? '' : styles.panelClosing}`}
      aria-label="工作面板"
      inert={!open}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && event.target instanceof Element && event.target.closest('header, nav, [data-panel-navigation]')) {
          event.preventDefault()
          event.stopPropagation()
          requestClose()
        }
      }}
      data-resizing={resizing}
      style={{ '--panel-width': `${String(width)}px` } as React.CSSProperties}
    >
      <div className={styles.resizer} {...handle} role="separator" aria-label="拖动调整面板宽度" aria-orientation="vertical" title="拖动调整宽度，双击恢复默认；方向键微调" />
      <div className={styles.surface}>
        <header className={styles.head}>
          <span className={styles.headTitle}>工作面板</span>
          <Button variant="icon" size="sm" aria-label="关闭面板" title="关闭面板" onClick={requestClose}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
              <path d="M6 6l12 12M18 6 6 18" />
            </svg>
          </Button>
        </header>
        {cleanupError ? <div role="alert" className={styles.filesSideHead}><p className={styles.hint}>{cleanupError}</p>
          <Button size="sm" variant="ghost" onClick={() => setCleanupError(null)}>知道了</Button></div> : null}
        <div className={styles.tabBar} data-panel-navigation>
          <button ref={homeRef} type="button" className={`${styles.homeButton} ${active === null ? styles.homeActive : ''}`}
            aria-label="工作面板首页" aria-pressed={active === null} title="工作面板首页" onClick={() => selectTab(null)}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z" />
            </svg>
          </button>
          <div className={styles.tabViewport}>
          <div ref={tabStripRef} className={styles.tabStrip} role="tablist" aria-label="工作面板页签">
            {tabs.map((item) => {
              const label = tabLabel(item)
              const entry = MENU.find((entry) => entry.id === item.kind)!
              return (
                <span
                  key={item.id}
                  className={item.id === activeId ? `${styles.chip} ${styles.chipActive}` : styles.chip}
                >
                  <button
                    type="button"
                    role="tab"
                    id={`panel-tab-${item.id}`}
                    aria-controls={item.id === activeId ? `panel-content-${item.id}` : undefined}
                    aria-selected={item.id === activeId}
                    tabIndex={item.id === activeId || (activeId === null && item.id === tabs[0]?.id) ? 0 : -1}
                    ref={(element) => { if (element) tabRefs.current.set(item.id, element); else tabRefs.current.delete(item.id) }}
                    title={item.kind === 'browser' ? label : item.param ?? label}
                    className={styles.chipLabel}
                    onClick={() => selectTab(item.id)}
                    onKeyDown={(event) => {
                      const index = tabs.findIndex((tab) => tab.id === item.id)
                      const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length
                        : event.key === 'ArrowLeft' ? (index - 1 + tabs.length) % tabs.length
                        : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
                      if (event.key === 'Delete') { event.preventDefault(); closeTab(item.id); return }
                      if (next === null) return
                      event.preventDefault()
                      selectTab(tabs[next].id)
                    }}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d={entry.icon} /></svg>
                    <span>{label}</span>
                  </button>
                  <button
                    type="button"
                    className={styles.chipClose}
                    aria-label={`关闭${label}页`}
                    tabIndex={item.id === activeId ? 0 : -1}
                    onClick={() => closeTab(item.id)}
                  >
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                      <path d="M6 6l12 12M18 6 6 18" />
                    </svg>
                  </button>
                </span>
              )
            })}
          </div>
          {tabScroll.overflow ? <button type="button" className={`${styles.tabScroll} ${styles.tabScrollLeft}`} aria-disabled={!tabScroll.left}
            aria-label="向左滚动标签" title="向左滚动标签" onClick={() => { if (tabScroll.left) scrollTabs(-1) }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m14 6-6 6 6 6" /></svg>
          </button> : null}
          {tabScroll.overflow ? <button type="button" className={styles.tabScroll} aria-disabled={!tabScroll.right}
            aria-label="向右滚动标签" title="向右滚动标签" onClick={() => { if (tabScroll.right) scrollTabs(1) }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m10 6 6 6-6 6" /></svg>
          </button> : null}
          </div>
          <Button
            variant="icon"
            size="sm"
            aria-label="新建页签"
            title="新建页签"
            onClick={() => selectTab(null)}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </Button>
        </div>

        {active === null ? (
          <nav className={styles.menu} aria-label="工作面板功能">
            <div className={styles.menuIntro}>
              <span className={styles.menuEyebrow}>当前会话 · 工作区工具</span>
              <h2 className={styles.menuTitle}>在这里展开工作</h2>
              <p className={styles.menuLead}>浏览文件、查看改动，或开启新的工作标签。</p>
            </div>
            {MENU.map((item, index) => (
              <button
                key={item.id}
                type="button"
                className={styles.menuItem}
                aria-label={item.label}
                aria-describedby={`panel-menu-hint-${item.id}`}
                disabled={item.id === 'sidechat' && sessionId === null}
                style={{ '--item-index': index } as React.CSSProperties}
                onClick={() => openTab(item.id)}
              >
                <span className={styles.menuIcon} aria-hidden="true">
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d={item.icon} />
                  </svg>
                </span>
                <span className={styles.menuText}>
                  <span className={styles.menuLabel}>{item.label}</span>
                  <span id={`panel-menu-hint-${item.id}`} className={styles.menuHint}>{item.id === 'sidechat' && sessionId === null ? '先打开一个主会话，再开启侧边对话' : item.hint}</span>
                </span>
                <svg className={styles.menuArrow} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true"><path d="m9 5 7 7-7 7" /></svg>
              </button>
            ))}
            <p className={styles.menuFooter}>通过右上角 + 打开更多标签，保留当前工作。</p>
          </nav>
        ) : null}

        {active === null ? null : (
          <div key={active.id} id={`panel-content-${active.id}`} role="tabpanel" aria-labelledby={`panel-tab-${active.id}`}
            data-slide-direction={slideDirection}
            tabIndex={0} className={`${styles.body} ${active.kind === 'files' || active.kind === 'changes' || active.kind === 'sidechat' || active.kind === 'browser' ? styles.flushBody : ''} ${active.kind === 'terminal' ? styles.terminalBody : ''}`}>
            {active.kind === 'browser' ? <Suspense fallback={<p className={styles.hint}>正在加载浏览器…</p>}>
              <BrowserPane requestId={active.browserKey!} workspaceId={workspaceId}
                onSession={(id, title) => setTabs(previous => previous.map(item => item.id !== active.id || (item.param === id && item.title === title)
                  ? item : { ...item, param: id, title }))} />
            </Suspense> : null}
            {active.kind === 'files' ? (
              <FilesTab
                workspaceId={workspaceId}
                openFile={active.param}
                onOpenFile={(path) => patchTab(active.id, path)}
              />
            ) : null}
            {active.kind === 'changes' ? (
              <ChangesTab
                sessionId={sessionId}
                focusPath={active.param ?? undefined}
                onSelectFile={(path) => patchTab(active.id, path)}
              />
            ) : null}
            {active.kind === 'steps' ? <StepsTab trace={trace} onOpenStep={onOpenStep} /> : null}
            {active.kind === 'sidechat' ? (
              <SideChatPane
                parentSessionId={sessionId}
                sessionId={active.param}
                onStart={() => createSideChat(active.id)}
                onSession={(id) => patchTab(active.id, id)}
                draft={sideDrafts[active.id] ?? ''}
                onDraftChange={(update) => setSideDrafts((previous) => ({ ...previous,
                  [active.id]: typeof update === 'function' ? update(previous[active.id] ?? '') : update }))}
              />
            ) : null}
            {active.kind === 'terminal' ? (
              active.param === null ? (
                <p className={styles.hint}>{terminalIssues[active.id] ?? '正在启动终端…'}</p>
              ) : (
                <Suspense fallback={<p>正在加载终端…</p>}>
                  <TerminalPane key={`${workspaceId ?? ''}-${active.id}`} terminalId={active.param} />
                </Suspense>
              )
            ) : null}
          </div>
        )}
      </div>
    </aside>
  )
}

function FilesTab({ workspaceId, openFile, onOpenFile }: {
  workspaceId: string | null
  /** 当前打开的文件：提到页签的 param 里，切页签回来才不会丢。 */
  openFile: string | null
  onOpenFile: (path: string | null) => void
}) {
  const [filter, setFilter] = useState('')
  // 树宽可拖（2026-10-23）：深目录的路径很长，固定 220px 不够看，窄屏又太占。
  const { width: treeWidth, handle: treeHandle } = usePaneResize(220, 140, 420)
  // 一次性取**整棵树**（`recursive`）：树状展示下不再有"逐层点进去"（2026-10-23 实机反馈）。
  const listing = useWorkspaceFiles('', workspaceId !== null, true, workspaceId ?? undefined)
  // 输入关键词就切到**全项目搜索**：只筛当前目录的话，找一个文件要逐层点进去。
  const search = useSearchWorkspaceFiles(filter, workspaceId ?? undefined)
  const searching = filter.trim() !== ''
  // 预览必须带所属工作区：不传会按"启动工作区"解析，在别的会话工作区里必然读不到文件。
  const content = useArtifactsDetail(openFile, workspaceId ?? undefined)

  // 搜索模式用服务端结果（已过滤），否则用整棵树。两个分支都要用，所以提到最前。
  const treeEntries = searching
    ? (search.data?.entries ?? [])
        .filter((entry) => entry.kind === 'file')
        .map((entry) => ({
          path: entry.path ?? entry.name, kind: 'file' as const, size: entry.size_bytes,
        }))
    : (listing.data?.entries ?? []).map((entry) => ({
        path: entry.path ?? entry.name, kind: entry.kind, size: entry.size_bytes,
      }))

  if (workspaceId === null) return <PanelEmpty kind="files" title="尚未选择工作区" hint="选择工作区后，在这里浏览和预览文件。" />

  if (openFile !== null) {
    return (
      <div className={styles.filesSplit}>
        <div className={styles.filesMain}>
          <div className={styles.filesHead}>
            <FileIcon path={openFile} />
            <code className={styles.pathText} title={openFile}>{openFile}</code>
            <Button size="sm" variant="ghost" onClick={() => onOpenFile(null)}>关闭</Button>
          </div>
          <div className={styles.filesBody}>
            {content.isPending ? <p className={styles.hint}>正在读取…</p> : null}
            {content.isError ? <div role="alert">
              <p className={styles.hint}>{content.error instanceof ApiError
                ? content.error.message : '读不到这个文件：它可能已被删除，或不在当前工作区内。'}</p>
              <Button size="sm" variant="ghost" onClick={() => void content.refetch()}>重试</Button>
            </div> : null}
            {content.data?.kind === 'image' ? (
              <img className={styles.image} src={content.data.url ?? undefined} alt="" />
            ) : null}
            {content.data?.kind === 'text' ? (
              <HighlightedText text={content.data.text ?? ''} path={openFile} />
            ) : null}
            {content.data?.kind === 'text' && content.data.truncated ? (
              <p className={styles.hint}>
                文件 {formatSize(content.data.size_bytes)}，只显示开头部分。
              </p>
            ) : null}
            {content.data?.kind === 'too_large' ? (
              <p className={styles.hint}>
                图片 {formatSize(content.data.size_bytes)}，超过预览上限（2 MB）。
              </p>
            ) : null}
            {content.data?.kind === 'unreadable' ? <p className={styles.hint}>这个文件读不出来。</p> : null}
            {/* 二进制（2026-10-23）：以前按文本解码，docx 是一屏乱码。现在明确说不支持。 */}
            {content.data?.kind === 'binary' ? (
              <p className={styles.hint}>
                这是 .{content.data.suffix} 文件（{formatSize(content.data.size_bytes)}），二进制内容无法预览。
              </p>
            ) : null}
          </div>
        </div>
        <div className={styles.filesResizer} {...treeHandle} role="separator"
          aria-label="拖动调整文件树宽度" aria-orientation="vertical" />
        <FileTreeSide
          width={treeWidth}
          entries={treeEntries}
          onSelectFile={onOpenFile}
          selectedPath={openFile}
          filter={filter}
          onFilter={setFilter}
          searching={searching}
          searchFetching={search.isFetching}
          truncated={listing.data?.truncated === true}
          error={searching ? search.isError : listing.isError}
          pending={listing.isPending && !searching}
          onRetry={() => { void (searching ? search.refetch() : listing.refetch()) }}
        />
      </div>
    )
  }

  return (
    <div className={styles.filesSplit}>
      <div className={styles.filesMain}>
        <div className={styles.filesEmpty}>
          <span className={styles.filesEmptyIcon} aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2">
              <path d="M6 3h8l4 4v14H6z" />
              <path d="M14 3v4h4" />
            </svg>
          </span>
          <p className={styles.filesEmptyTitle}>文件</p>
          <p className={styles.filesEmptyHint}>从文件树中选择文件即可在此处预览</p>
        </div>
      </div>
      <div className={styles.filesResizer} {...treeHandle} role="separator"
        aria-label="拖动调整文件树宽度" aria-orientation="vertical" />
      <FileTreeSide
        width={treeWidth}
        entries={treeEntries}
        onSelectFile={onOpenFile}
        selectedPath={openFile}
        filter={filter}
        onFilter={setFilter}
        searching={searching}
        searchFetching={search.isFetching}
        truncated={listing.data?.truncated === true}
        error={searching ? search.isError : listing.isError}
        pending={listing.isPending && !searching}
        onRetry={() => { void (searching ? search.refetch() : listing.refetch()) }}
      />
    </div>
  )
}

/**
 * 文件树窄栏（2026-10-23）。
 *
 * 放在预览区**右侧**、固定宽度：点文件只换左边的内容，树不跳走——
 * "点一下就跳进去、再点返回"是上一版被否掉的交互。
 */
/**
 * 带语法高亮的文本预览（2026-10-23）。
 *
 * `shiki` 输出的是**它自己生成的 HTML**（不是用户内容），`dangerouslySetInnerHTML` 在这里安全。
 * 语言认不出或高亮失败时 `useHighlight` 返回 null，退回纯文本 `<pre>`。
 */
function HighlightedText({ text, path }: { text: string; path: string }) {
  const html = useHighlight(text, languageOf(path))
  if (html === null) return <pre className={styles.pre}>{text}</pre>
  return <div className={styles.highlight} dangerouslySetInnerHTML={{ __html: html }} />
}

function FileTreeSide({
  width, entries, onSelectFile, selectedPath, filter, onFilter,
  searching, searchFetching, truncated, error, pending, onRetry,
}: {
  width: number
  entries: React.ComponentProps<typeof FileTree>['entries']
  onSelectFile: (path: string) => void
  selectedPath?: string | null
  filter: string
  onFilter: (value: string) => void
  searching: boolean
  searchFetching: boolean
  truncated: boolean
  error?: boolean
  pending?: boolean
  onRetry: () => void
}) {
  return (
    <aside className={styles.filesSide} style={{ width: `${String(width)}px` }}>
      <div className={styles.filesSideHead}>
        <SearchField
          value={filter}
          onChange={onFilter}
          placeholder="搜索文件名…"
          aria-label="搜索文件名"
        />
      </div>
      {error === true ? <div role="alert" className={styles.filesSideHead}>
        <p className={styles.hint}>{searching ? '搜索失败，请重试。' : '文件树读取失败，请重试。'}</p>
        <Button size="sm" variant="ghost" onClick={onRetry}>重试</Button>
      </div> : null}
      {pending === true || (searching && searchFetching) ? <p role="status" className={styles.hint}>{searching ? '正在搜索…' : '正在读取…'}</p> : null}
      <div className={styles.treeScroll}>
        {error !== true && pending !== true && !(searching && searchFetching) ? <FileTree
          entries={entries}
          onSelectFile={onSelectFile}
          selectedPath={selectedPath}
          emptyHint={searching ? `没有匹配「${filter}」的文件。` : '这个工作区里还没有文件。'}
        /> : null}
      </div>
      {truncated ? <p className={styles.hint}>文件太多，只列出前 4000 项。</p> : null}
    </aside>
  )
}

function ChangesTab({ sessionId, focusPath, onSelectFile }: {
  sessionId: string | null
  /** 当前选中的文件：存在页签的 param 里。 */
  focusPath?: string
  onSelectFile: (path: string | null) => void
}) {
  const changes = useSessionChanges(sessionId)
  const selected = focusPath ?? null
  const [filter, setFilter] = useState('')
  const { width: treeWidth, handle: treeHandle } = usePaneResize(220, 140, 420)

  if (sessionId === null) {
    return <PanelEmpty kind="changes" title="尚未打开会话" hint="选择一个会话后，在这里查看文件改动。" />
  }

  // 账本里有改前内容的文件（能看完整对比）+ 被命令动过、没有基线的文件。
  const recorded: string[] = []
  for (const turn of changes.data?.turns ?? []) {
    for (const file of turn.files ?? []) {
      if (!recorded.includes(file.path)) recorded.push(file.path)
    }
  }
  const unrecorded = (changes.data?.unrecorded ?? []).map((item) => item.path)
  const all = [...recorded, ...unrecorded.filter((path) => !recorded.includes(path))]
  const needle = filter.trim().toLowerCase()
  const entries = all.filter((path) => needle === '' || path.toLowerCase().includes(needle))

  return (
    <div className={styles.filesSplit}>
      <section className={styles.filesMain} aria-label="改动详情">
        <div className={styles.filesHead}>
          {selected !== null ? <FileIcon path={selected} /> : null}
          <code className={styles.pathText} title={selected ?? undefined}>{selected ?? '改动详情'}</code>
          {selected !== null ? <Button size="sm" variant="ghost" aria-label="关闭改动详情" onClick={() => onSelectFile(null)}>关闭</Button> : null}
        </div>
        {selected !== null ? <div key={selected} className={styles.filesBody}>
          <FileDiff sessionId={sessionId} path={selected} />
        </div> : <PanelEmpty kind="changes"
            title={changes.isPending ? '正在读取改动…' : changes.isError ? '暂时无法读取改动' : all.length === 0 ? '还没有文件改动' : '选择一个改动文件'}
            hint={changes.isError ? '请在目录中重试。' : all.length === 0 ? '本次会话产生文件改动后，会在这里展示前后对比。' : '从目录中选择文件，在这里查看改前与改后的对比。'} />
        }
      </section>
      <div className={styles.filesResizer} {...treeHandle} role="separator"
        aria-label="拖动调整改动目录宽度" aria-orientation="vertical" />
      <aside className={styles.filesSide} style={{ width: `${String(treeWidth)}px` }} aria-label="改动目录">
        <div className={styles.filesSideHead}>
          <div className={styles.changesTreeHead}>
            <span>改动 <span className={styles.changesCount}>{all.length}</span></span>
            <Button size="sm" variant="ghost" onClick={() => void changes.refetch()}>刷新</Button>
          </div>
          <SearchField value={filter} onChange={setFilter} placeholder="按路径筛选…" aria-label="按路径筛选改动" />
        </div>
        {changes.isPending ? <p role="status" className={styles.hint}>正在读取改动…</p> : null}
        {changes.isError ? <div role="alert" className={styles.filesSideHead}>
          <p className={styles.hint}>改动记录读取失败。</p>
          <Button size="sm" variant="ghost" onClick={() => void changes.refetch()}>重试</Button>
        </div> : null}
        <div className={styles.treeScroll}>
          {!changes.isPending && !changes.isError ? <FileTree
            entries={entries.map((path) => ({ path, kind: 'file' as const }))}
            onSelectFile={onSelectFile}
            selectedPath={selected}
            emptyHint={needle === '' ? '暂无改动文件。' : '没有匹配的改动'}
          /> : null}
        </div>
        <div className={styles.changesTreeFoot}>
          <p className={styles.hint}>本次会话 · 只读对比</p>
          {changes.data?.unrecorded_note ? <p className={styles.hint}>{changes.data.unrecorded_note}</p> : null}
        </div>
      </aside>
    </div>
  )
}

/**
 * 步骤（2026-10-22 用户反馈后重做）。
 *
 * 原来是「任务中心」的后台任务列表，每行只有会话名——同一个会话里的任务名一模一样，
 * 看不出任何信息。用户原话："现在展示这个完全没用"。
 * 现在改成本次会话的**执行步骤**：第几轮、这轮要做什么、几个动作，点一步跳轨迹页看完整调用。
 */
function StepsTab({ trace, onOpenStep }: {
  trace: TraceViewModel
  onOpenStep?: (turnId: string) => void
}) {
  const turns = trace.turns

  return (
    <div className={styles.stack}>
      <p className={styles.note}>这次会话分了几步。点一步滚到对话里的那一轮。</p>
      {turns.length === 0 ? <PanelEmpty kind="steps" title="还没有执行步骤" hint="开始对话后，在这里查看每轮执行过程并跳转。" /> : null}
      <ul className={styles.list}>
        {turns.map((turn, index) => (
          <li key={turn.id}>
            {/* 用轮首消息 id：对话里 `data-turn-id` 挂的是轮首，`turn.id` 是轮尾。 */}
            <button type="button" className={styles.row} title={turn.prompt || '（这一轮没有文字输入）'} onClick={() => onOpenStep?.(turn.leadId ?? turn.id)}>
              <span className={styles.stepIndex} aria-hidden="true">{index + 1}</span>
              <span className={styles.name}>{turn.prompt || '（这一轮没有文字输入）'}</span>
              {/* 没有工具调用就不显示"0 个动作"：纯聊天轮次占多数，全列 0 只是噪音。 */}
              {turn.rows.length > 0 ? <span className={styles.size}>{turn.rows.length} 个动作</span> : null}
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function PanelEmpty({ kind, title, hint }: { kind: Tab; title: string; hint: string }) {
  return <div className={styles.filesEmpty}>
    <span className={styles.filesEmptyIcon} aria-hidden="true">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
        <path d={MENU.find((entry) => entry.id === kind)?.icon} />
      </svg>
    </span>
    <p className={styles.filesEmptyTitle}>{title}</p>
    <p className={styles.filesEmptyHint}>{hint}</p>
  </div>
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
