import { lazy, Suspense, useEffect, useRef, useState } from 'react'

import { useArtifactsDetail, useSearchWorkspaceFiles, useSessionChanges, useWorkspaceFiles } from '../../api/queries'
import type { TraceView as TraceViewModel } from '../../fixtures/types'
import { Button } from '../../components/ui/Button'
import { SearchField } from '../../components/ui/SearchField'
import { FileDiff } from '../code-changes/ChangesPanel'
import { FileIcon } from './fileIcon'
import { languageOf, useHighlight } from './useHighlight'
import { FileTree } from './FileTree'
import { SideChatPane } from './SideChatPane'
import styles from './PanelShell.module.css'

const TerminalPane = lazy(() => import('./TerminalPane'))

const DEFAULT_WIDTH = 620
const MIN_WIDTH = 320
const MAX_WIDTH = 760

type Tab = 'files' | 'changes' | 'steps' | 'sidechat' | 'terminal'

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
  { id: 'terminal', label: '终端', hint: '运行命令及脚本', icon: 'M4 6h16v12H4zM8 10l2 2-2 2' },
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
      .map((item) => ({ id: item.id, kind: item.kind, param: typeof item.param === 'string' ? item.param : null }))
    if (tabs.length === 0) return null
    const activeId = tabs.some((item) => item.id === parsed.activeId)
      ? (parsed.activeId ?? null)
      : tabs[tabs.length - 1].id
    const width = typeof parsed.width === 'number'
      ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, parsed.width))
      : DEFAULT_WIDTH
    return { tabs, activeId, width }
  } catch {
    // 存储被写坏（手工改过、旧版本残留）就当没有，不能让整页打不开。
    return null
  }
}

export function PanelShell({
  workspaceId,
  sessionId,
  trace,
  onOpenStep,
  onClose,
  focus,
}: {
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
  const restored = useRef<PersistedPanel | null>(loadPanelState(sessionId))
  const [tabs, setTabs] = useState<PanelTab[]>(restored.current?.tabs ?? [])
  const [activeId, setActiveId] = useState<string | null>(restored.current?.activeId ?? null)
  // 序号要接着恢复出来的最大值往后走：从 0 重新数会和已存在的 `files-1` 撞 id，
  // 撞了之后 React 的 key 重复、切页签会切到错的那页。
  const counter = useRef(restored.current === null ? 0 : Math.max(0,
    ...restored.current.tabs.map((item) => {
      const matched = /-(\d+)$/.exec(item.id)
      return matched === null ? 0 : Number(matched[1])
    })))
  const active = tabs.find((item) => item.id === activeId) ?? null

  const openTab = (kind: Tab, param?: string) => {
    counter.current += 1
    const id = `${kind}-${String(counter.current)}`
    setTabs((previous) => [...previous, { id, kind, param: param ?? null }])
    setActiveId(id)
    return id
  }

  const patchTab = (id: string, param: string | null) => {
    setTabs((previous) => previous.map((item) => (item.id === id ? { ...item, param } : item)))
  }

  const closeTab = (id: string) => {
    setTabs((previous) => {
      const next = previous.filter((item) => item.id !== id)
      if (activeId === id) setActiveId(next.length === 0 ? null : next[next.length - 1].id)
      return next
    })
  }

  // 宽度可拖（2026-10-23）：固定宽度在宽屏上浪费、在窄屏上挤，交给用户自己拉。
  const [width, setWidth] = useState(restored.current?.width ?? DEFAULT_WIDTH)

  // 任何变化都落盘（关掉页面 / 刷新后由 `loadPanelState` 读回来）。
  useEffect(() => {
    try {
      localStorage.setItem(storageKey(sessionId), JSON.stringify({
        version: STORAGE_VERSION, tabs, activeId, width,
      }))
    } catch {
      // 存储写满或被禁用：功能照用，只是记不住。不该因此报错打断用户。
    }
  }, [tabs, activeId, width, sessionId])

  const startDrag = (event: React.MouseEvent<HTMLDivElement>) => {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = width
    const move = (moveEvent: MouseEvent) => {
      // 右栏贴右边，往左拖才是变宽。
      const next = startWidth + (startX - moveEvent.clientX)
      setWidth(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, next)))
    }
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      document.body.style.userSelect = ''
    }
    // 拖动时禁掉文本选择，否则整页会像被"框选"了一样。
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  // 从对话流的改动条点进来：已有改动页就切过去并定位文件，没有就开一个。
  useEffect(() => {
    if (!focus) return
    const existing = tabs.find((item) => item.kind === focus.tab)
    if (existing !== undefined) {
      setActiveId(existing.id)
      patchTab(existing.id, focus.path)
      return
    }
    openTab(focus.tab, focus.path)
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只在 focus 变化时触发
  }, [focus])

  return (
    <aside
      className={styles.panel}
      aria-label="工作面板"
      style={{ width: `${String(width)}px` }}
    >
      {/** 左侧边缘的拖拽手柄：只改宽度，不改别的 */}
      <div className={styles.resizer} onMouseDown={startDrag} role="separator" aria-label="拖动调整面板宽度" aria-orientation="vertical" />
      <header className={styles.head}>
        {/* 菜单态只留「+」和关闭：标题"工作面板"在参考图里没有，占地方也没信息量。 */}
        <div className={styles.tabStrip} role="tablist" aria-label="工作面板页签">
          {tabs.map((item) => {
            const label = MENU.find((entry) => entry.id === item.kind)?.label ?? item.kind
            return (
              <span
                key={item.id}
                className={item.id === activeId ? `${styles.chip} ${styles.chipActive}` : styles.chip}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={item.id === activeId}
                  className={styles.chipLabel}
                  onClick={() => setActiveId(item.id)}
                >
                  {label}
                </button>
                <button
                  type="button"
                  className={styles.chipClose}
                  aria-label={`关闭${label}页`}
                  onClick={() => closeTab(item.id)}
                >
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                    <path d="M6 6l12 12M18 6 6 18" />
                  </svg>
                </button>
              </span>
            )
          })}
          <Button
            variant="icon"
            size="sm"
            aria-label="新建页签"
            title="新建页签"
            onClick={() => setActiveId(null)}
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </Button>
        </div>
        <Button variant="icon" size="sm" aria-label="关闭面板" title="关闭" onClick={onClose}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" />
          </svg>
        </Button>
      </header>

      {active === null ? (
        <nav className={styles.menu} aria-label="工作面板功能">
          <p className={styles.menuLead}>从这里开始</p>
          {MENU.map((item) => (
            <button
              key={item.id}
              type="button"
              className={styles.menuItem}
              onClick={() => openTab(item.id)}
            >
              <span className={styles.menuIcon} aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                  <path d={item.icon} />
                </svg>
              </span>
              {/* 名称与说明**同一行**（2026-10-23 对齐参考图）：说明是补充，不该独占一行。 */}
              <span className={styles.menuLabel}>{item.label}</span>
              <span className={styles.menuHint}>{item.hint}</span>
            </button>
          ))}
        </nav>
      ) : null}

      {active === null ? null : (
        <div className={`${styles.body} ${active.kind === 'terminal' ? styles.terminalBody : ''}`}>
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
              onSession={(id) => patchTab(active.id, id)}
            />
          ) : null}
          {active.kind === 'terminal' ? (
            <Suspense fallback={<p>正在加载终端…</p>}>
              <TerminalPane key={`${workspaceId ?? ''}-${active.id}`} workspaceId={workspaceId} />
            </Suspense>
          ) : null}
        </div>
      )}
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
  const [treeWidth, setTreeWidth] = useState(220)
  const startTreeDrag = (event: React.MouseEvent<HTMLDivElement>) => {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = treeWidth
    const move = (moveEvent: MouseEvent) => {
      // 树在右边，往左拖才是变宽。
      setTreeWidth(Math.min(420, Math.max(140, startWidth + (startX - moveEvent.clientX))))
    }
    const up = () => {
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
      document.body.style.userSelect = ''
    }
    document.body.style.userSelect = 'none'
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }
  // 一次性取**整棵树**（`recursive`）：树状展示下不再有"逐层点进去"（2026-10-23 实机反馈）。
  const listing = useWorkspaceFiles('', openFile === null, true, workspaceId ?? undefined)
  // 输入关键词就切到**全项目搜索**：只筛当前目录的话，找一个文件要逐层点进去。
  const search = useSearchWorkspaceFiles(filter)
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
            {content.isError ? (
              <p className={styles.hint}>读不到这个文件：它可能已被删除，或不在当前工作区内。</p>
            ) : null}
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
        <div className={styles.filesResizer} onMouseDown={startTreeDrag} role="separator"
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
      <div className={styles.filesResizer} onMouseDown={startTreeDrag} role="separator"
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
        error={listing.isError}
        pending={listing.isPending && !searching}
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
  searching, searchFetching, truncated, error, pending,
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
      {error === true ? <p className={styles.hint}>文件树读不到，可能已被删除。</p> : null}
      {pending === true ? <p className={styles.hint}>正在读取…</p> : null}
      {searching && searchFetching ? <p className={styles.hint}>正在搜索…</p> : null}
      <div className={styles.treeScroll}>
        <FileTree
          entries={entries}
          onSelectFile={onSelectFile}
          selectedPath={selectedPath}
          emptyHint={searching ? `没有匹配「${filter}」的文件。` : '这个工作区里还没有文件。'}
        />
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

  if (sessionId === null) {
    return (
      <div className={styles.stack}>
        <p className={styles.note}>本次会话改过的文件。只读，不会 stage 或提交。</p>
        <p className={styles.hint}>先打开一个会话。</p>
      </div>
    )
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

  if (selected !== null) {
    return (
      <div className={styles.stack}>
        <div className={styles.crumbs}>
          <button type="button" className={styles.back} onClick={() => onSelectFile(null)}>← 返回</button>
          <code className={styles.pathText}>{selected}</code>
        </div>
        {/* 始终走 DiffCard：红绿行与语法着色都在。裸 <pre> 显示 patch 会丢光这两样。 */}
        <FileDiff sessionId={sessionId} path={selected} />
      </div>
    )
  }

  return (
    <div className={styles.stack}>
      <p className={styles.note}>本次会话改过的文件（改前 → 改后）。只读，不会 stage 或提交。</p>
      <div className={styles.crumbs}>
        <span className={styles.pathText}>{all.length} 个文件</span>
        <Button size="sm" variant="ghost" onClick={() => void changes.refetch()}>刷新</Button>
      </div>
      {all.length > 6 ? (
        <SearchField
          value={filter}
          onChange={setFilter}
          placeholder="按路径筛选…"
          aria-label="按路径筛选改动"
        />
      ) : null}
      {changes.isPending ? <p className={styles.hint}>正在读取…</p> : null}
      {changes.isError ? <p className={styles.hint}>改动记录读不到，稍后再试。</p> : null}
      <div className={styles.treeScroll}>
        {/* 同样走树：列一堆扁平路径看不出"改了哪个目录下的哪几处"（2026-10-23 实机反馈）。 */}
        <FileTree
          entries={entries.map((path) => ({ path, kind: 'file' as const }))}
          onSelectFile={onSelectFile}
          selectedPath={selected}
          emptyHint={needle === ''
            ? '本次会话还没有改动任何文件。'
            : `没有匹配「${filter}」的改动。`}
        />
      </div>
      {changes.data?.unrecorded_note ? <p className={styles.hint}>{changes.data.unrecorded_note}</p> : null}
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
      {turns.length === 0 ? <p className={styles.hint}>这次会话还没有可展示的步骤。</p> : null}
      <ul className={styles.list}>
        {turns.map((turn, index) => (
          <li key={turn.id}>
            {/* 用轮首消息 id：对话里 `data-turn-id` 挂的是轮首，`turn.id` 是轮尾。 */}
            <button type="button" className={styles.row} onClick={() => onOpenStep?.(turn.leadId ?? turn.id)}>
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

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
