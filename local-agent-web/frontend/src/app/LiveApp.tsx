// 真实会话壳（T30）：选择工作区 → 会话列表 → 打开会话 → 提交 → 增量回答 → 刷新恢复。
//
// 分工（计划 §6.3）：
// - **服务端权威**：工作区、会话列表、快照、连接列表 → React Query（`api/queries.ts`）；
// - **本地 UI**：选中项与逐会话草稿 → Zustand（`stores/ui.ts`）；
// - **流式尾部**：临时增量 → `stores/stream.ts` + `useSessionStream`；
// - **展示**：一律经 `adapters.ts` 映射成纯 view model，组件仍然不认 DTO。
//
// 刷新恢复**不靠本地存储**：打开哪个会话由服务端的 `is_active` 决定（见下面的回填），
// 会话内容由快照决定。这是 T30 停止条件"前端必须读取 State 才能恢复"的反面。

import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { Notice } from '../components/Notice'
import { Composer } from '../components/Composer'
import { Sidebar } from '../components/Sidebar'
import { TopBar } from '../components/TopBar'
import { Conversation } from '../features/conversation/Conversation'
import { WorkflowStatus } from '../features/workflow/WorkflowStatus'
import { mapWorkflow } from '../features/workflow/adapters'
import { toolLabel } from '../features/tool/display'
import {
  mapApproval,
  mapCall,
  mapContext,
  mapError,
  mapMessages,
  collapseDuplicateErrors,
  mapPlan,
  mapSession,
  mapStats,
  mapTurnStats,
  modelLabelFor,
} from '../features/conversation/adapters'
import { mapSidebar, sessionTaskStates } from '../features/sidebar/adapters'
import { toTasksView } from '../api/viewModels'
import { ModelSettingsDialog } from '../features/settings/ModelSettingsDialog'
import { parseSelectionVersion, selectionExpectation, toModelMenu } from '../features/settings/modelMenuAdapter'
import { traceFromMessages } from '../features/trace/adapters'
import { TraceView } from '../features/trace/TraceView'
import { ConversationMapPanel } from '../features/conversation-map/ConversationMapPanel'
import { TurnChanges } from '../features/code-changes/TurnChanges'
import { DataCenter } from '../features/data-center/DataCenter'
import { CapabilitiesCenter } from '../features/capabilities/CapabilitiesCenter'
import { CommandPalette } from '../features/commands/CommandPalette'
import { CronPanel } from '../features/cron/CronPanel'
import { ArtifactsPanel } from '../features/artifacts/ArtifactsPanel'
import { PanelShell } from '../features/panel/PanelShell'
import { TasksSection } from '../features/tasks/TasksSection'
import type { components } from '../api/schema.gen'
import { ApiError, archiveSession, promoteSideChat, detachCommand as requestDetachCommand, listSessions, removeWorkspace, renameSession, renameWorkspace, restoreSession, selectSession, uploadAttachment } from '../api/client'
import { queryKeys, useHealth, useModelConnections, useSessions, useSnapshot, useSubmitMessage, useTasks, useWorkspaces, useCreateSession, useSelectWorkspace, useSelectSessionModel, useResolveApproval, usePickWorkspaceDirectory, useSetSessionMode, useEndInterruptedRequest, useCancelTask, useCloseTaskReview } from '../api/queries'
import { useSessionStream } from '../api/useSessionStream'
import { useStreamTail } from '../stores/stream'
import { useUiStore } from '../stores/ui'
import type { ConversationScenario, MessageView } from '../fixtures/types'
import { prepareImageForUpload } from './imageCompression'
import styles from './App.module.css'
import { useArchivedSessions } from '../api/queries'

/**
 * 低于这个宽度侧栏自动收成 56px 窄轨。
 *
 * 268px 的侧栏加 760px 的会话列需要 1028px 才放得下；再窄就会把内容压出横向滚动。
 * 用户手动点过收起/展开之后以手动选择为准（见下面的 `collapsedPref`）。
 */
const NARROW_SIDEBAR_QUERY = '(max-width: 1024px)'

/** jsdom（单测环境）没有 `matchMedia`：拿不到就按宽窗处理，不抛异常。 */
function isNarrowViewport(): boolean {
  if (typeof window.matchMedia !== 'function') return false
  return window.matchMedia(NARROW_SIDEBAR_QUERY).matches
}

export function LiveApp() {
  const workspaceId = useUiStore((state) => state.workspaceId)
  const activeSessionId = useUiStore((state) => state.activeSessionId)
  const drafts = useUiStore((state) => state.drafts)
  const setWorkspace = useUiStore((state) => state.setWorkspace)
  const openSession = useUiStore((state) => state.openSession)
  const setDraft = useUiStore((state) => state.setDraft)

  // 侧栏折叠：`null` = 跟随窗口宽度，非 null = 用户手动选择过（手动优先）。
  const [collapsedPref, setCollapsedPref] = useState<boolean | null>(null)
  const [narrowViewport, setNarrowViewport] = useState(isNarrowViewport)

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia(NARROW_SIDEBAR_QUERY)
    const onChange = (event: MediaQueryListEvent) => setNarrowViewport(event.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])

  const sidebarCollapsed = collapsedPref ?? narrowViewport

  const [settingsOpen, setSettingsOpen] = useState(false)
  const [dataCenterOpen, setDataCenterOpen] = useState(false)
  // 「能力中心」整页（2026-10-22 V5）：与数据中台/任务中心互斥，同为纯界面状态。
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false)
  // 命令面板（2026-10-22 V7）：Ctrl/Cmd+K 开关，纯界面状态。
  const [paletteOpen, setPaletteOpen] = useState(false)
  // 定时任务整页（2026-10-22 J2）。
  const [cronOpen, setCronOpen] = useState(false)
  // 产物整页（2026-10-22）：从数据中台第六个 tab 提出来——埋在里面没人找得到。
  const [artifactsOpen, setArtifactsOpen] = useState(false)
  // 右栏工作面板（§21.6）：纯界面状态，刷新后收起。
  // 面板开合持久化（2026-10-23）：只记**用户主动**的开/关。
  // 切会话时的自动收起不写入，否则每切一次会话就把偏好改成「关」，下次启动永远打不开。
  const [panelOpen, setPanelOpen] = useState(() => {
    try { return localStorage.getItem('panel.open') === '1' } catch { return false }
  })
  const setPanelOpenPersisted = (next: boolean) => {
    setPanelOpen(next)
    try { localStorage.setItem('panel.open', next ? '1' : '0') } catch { /* 存不了就只是记不住 */ }
  }
  // 从对话流的改动条点进来时，指定面板要打开哪个文件的 diff。
  const [panelFocus, setPanelFocus] = useState<{ tab: 'changes'; path: string } | null>(null)
  // 换会话就收起右栏（2026-10-23 用户要求）：文件树、改动、侧聊全是"这个会话"的，
  // 留在屏幕上会让人以为看的是新会话的东西。
  useEffect(() => {
    setPanelOpen(false)
    setPanelFocus(null)
  }, [activeSessionId])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteOpen((open) => !open)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])
  // 右栏「变更预览」与对话区顶部改动条（2026-10-21）：纯界面状态，刷新后回到收起。
  // `changesPath` = 改动条里当前高亮的文件；切换会话时清空，避免拿 A 会话的文件名去问 B 会话。
  // 右栏 diff 面板 2026-10-22 按用户要求暂时下线（要按 hermes 重做），这里只保留高亮状态。
  const [changesPath, setChangesPath] = useState<string | null>(null)
  // 「任务中心」面板（v0.6 T62；§17.6）：同样是纯界面状态。数据由 `useTasks` 提供——
  // 它与面板内的查询**同一个 key**，因此角标与列表不会各自缓存出一份不同的结论。
  const [tasksOpen, setTasksOpen] = useState(false)
  const closeTaskReview = useCloseTaskReview()
  const [archivedOpen, setArchivedOpen] = useState(false)
  // 上下文详情面板的展开态：**界面状态**，不入服务端契约（刷新后回到收起）。
  const [meterOpen, setMeterOpen] = useState(false)
  const [modelMenuOpen, setModelMenuOpen] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  const [workspaceActionError, setWorkspaceActionError] = useState<string | null>(null)
  // 顶部页签：对话 / 轨迹。纯界面状态，刷新后回到对话页（不猜用户上次看的是哪一页）。
  const [tab, setTab] = useState<'conversation' | 'trace' | 'map'>('conversation')
  const [followup, setFollowup] = useState<{
    sessionId: string; turnId: string; question: string; ordinal: number;
    context: components['schemas']['SessionSnapshot']; baseSnapshotId: string; branchId: string;
  } | null>(null)
  const [mapChat, setMapChat] = useState<{ sessionId: string; turnId: string } | null>(null)
  const [focusTurn, setFocusTurn] = useState<{ sessionId: string; turnId: string } | null>(null)
  // 工具行的展开态：**纯界面状态**，不来自快照。T32 的结构化 diff 就在展开后才可见
  // （`ToolRow` 收起时只有一行摘要），所以真会话必须能让用户按需展开。
  const [expandedTools, setExpandedTools] = useState<ReadonlySet<string>>(() => new Set())
  const [dismissedErrors, setDismissedErrors] = useState<ReadonlySet<string>>(() => new Set())
  const errorActions = { dismissedIds: dismissedErrors, onToggle: (diagnosticId: string) => {
    setDismissedErrors((previous) => {
      const next = new Set(previous); if (!next.delete(diagnosticId)) next.add(diagnosticId); return next
    })
  } }
  const queryClient = useQueryClient()

  const health = useHealth()
  // 任务中心的数据源（v0.6 T62；§17.6）：入口角标与面板**共用同一个 query key**，
  // 因此不会各缓存一份不同的结论。角标直接用服务端给的"等你处理"计数，页面不自己加总。
  const tasks = useTasks(workspaceId)
  const workspaces = useWorkspaces()
  const sessions = useSessions(null)
  const archivedSessions = useArchivedSessions(archivedOpen)
  const snapshotQuery = useSnapshot(activeSessionId)
  const connections = useModelConnections()
  const createSession = useCreateSession()
  const setSessionMode = useSetSessionMode()
  const endInterrupted = useEndInterruptedRequest()
  const cancelTask = useCancelTask()
  const [modeError, setModeError] = useState<{ sessionId: string; message: string } | null>(null)
  const [recoveryConfirm, setRecoveryConfirm] = useState(false)
  const selectWorkspace = useSelectWorkspace()
  const pickWorkspace = usePickWorkspaceDirectory()
  const selectSessionModel = useSelectSessionModel(activeSessionId)
  const approvalDecision = useResolveApproval(activeSessionId)

  const stream = useSessionStream(activeSessionId)
  const tail = useStreamTail(activeSessionId)

  // 稳定引用：`?? []` 每次渲染都会产生新数组，会让下面的 effect/memo 每次都重跑。
  const sessionList = useMemo(() => sessions.data ?? [], [sessions.data])
  // 侧聊（§21.7）：标题栏要显示它从哪开出来，父会话可能已被删——那就退回一个通用说法。
  const activeSummary = sessionList.find((item) => item.session_id === activeSessionId)
  const sideChatParentTitle = activeSummary?.parent_session_id === undefined
    || activeSummary?.parent_session_id === null
    ? null
    : sessionList.find((item) => item.session_id === activeSummary.parent_session_id)?.title ?? '主会话'
  const snapshot = snapshotQuery.data ?? null
  const selectedFollowup = followup?.sessionId === activeSessionId ? followup : null
  const currentMode = snapshot?.mode ?? sessionList.find((item) => item.session_id === activeSessionId)?.mode ?? 'agent'
  const submit = useSubmitMessage(activeSessionId, {
    parent_turn_id: selectedFollowup?.turnId,
    expected_branch_id: selectedFollowup?.branchId ?? snapshot?.branch_id,
    expected_snapshot_id: selectedFollowup?.baseSnapshotId ?? snapshot?.snapshot_id,
    // 界面上不再有「执行方式」下拉（2026-10-13）：三类确定性工作流从输入区撤下，只保留内部/评测入口。
    // 提交仍显式带 `workflow: null`——服务端那个可选字段仍在（§18.5），非页面调用方照样能用。
    workflow: null,
  })

  // 刷新恢复：本地没有选中项时，按服务端的 `is_active` 回填（全库唯一 active）。
  useEffect(() => {
    if (activeSessionId !== null) return
    const active = sessionList.find((item) => item.is_active === true)
    if (active === undefined) return
    openSession(active.session_id)
    setWorkspace(workspaces.data?.some((item) => item.workspace_id === active.workspace.workspace_id)
      ? active.workspace.workspace_id : null)
  }, [activeSessionId, openSession, sessionList, setWorkspace, workspaces.data])

  // 没有显式工作区时跟随第一个：服务端可能已经有启动默认工作区。
  useEffect(() => {
    if (workspaceId !== null || activeSessionId !== null || sessionList.some((item) => item.is_active)) return
    const first = (workspaces.data ?? [])[0]
    if (first === undefined) return
    setWorkspace(first.workspace_id)
  }, [setWorkspace, workspaceId, workspaces.data, activeSessionId, sessionList])

  // 两份查询可能先后返回。始终按打开的会话与登记表对齐新会话的绑定目标。
  // 移除的目录保留历史，但不再成为侧栏的登记工作区。
  useEffect(() => {
    if (workspaces.data === undefined) return
    const selected = sessionList.find((item) => item.session_id === activeSessionId)
    if (selected === undefined) return
    const next = workspaces.data.some((item) => item.workspace_id === selected.workspace.workspace_id)
      ? selected.workspace.workspace_id : null
    if (workspaceId !== next) setWorkspace(next)
  }, [workspaceId, workspaces.data, setWorkspace, activeSessionId, sessionList])

  const modelLabel = snapshot === null ? '' : modelLabelFor(snapshot, connections.data ?? [])
  const modelMenu = snapshot === null || !modelMenuOpen
    ? null
    : toModelMenu(snapshot, connections.data ?? [])

  const sidebar = useMemo(
    () =>
      mapSidebar({
        workspaces: workspaces.data ?? [],
        sessions: sessionList,
        activeSessionId,
        activeStatus: snapshot === null ? null : mapSession(snapshot, modelLabel).status,
        // 其余行的"现在在做什么"来自**任务索引**（v0.8 T71；§19.1）：与入口角标、任务中心
        // 共用同一个 query，不新增端点、不新增真源；没有任务的行不显示状态。
        sessionTasks: sessionTaskStates(tasks.data === undefined ? null : toTasksView(tasks.data)),
      }),
    [activeSessionId, modelLabel, sessionList, snapshot, tasks.data, workspaces.data],
  )

  const draft = activeSessionId === null ? '' : (drafts[activeSessionId] ?? '')
  const session = snapshot === null ? null : mapSession(snapshot, modelLabel)
  const running = session?.status === 'running'
  const awaitingApproval = session?.status === 'awaiting-approval'
  const interrupted = snapshot?.interrupted_request_id != null
  useEffect(() => { setRecoveryConfirm(false) }, [activeSessionId, snapshot?.snapshot_id, snapshot?.interrupted_request_id])
  // 切换会话时清掉"正在对比的文件"：那是上一个会话的路径，留着会让右栏显示别人的文件名。
  useEffect(() => { setChangesPath(null) }, [activeSessionId])
  const failedWithoutDetail = snapshot?.session_state === 'failed' && (snapshot.errors ?? []).length === 0

  // 审批卡直接由快照派生：**刷新恢复不需要任何额外机制**——快照本来就由 Checkpoint 投影，
  // 挂起的审批只要还在后端就会重新出现在这里（T32 停止条件之一）。
  const approvalView = snapshot?.approval == null ? null : mapApproval(snapshot.approval)

  const scenario: ConversationScenario = useMemo(() => {
    const empty: ConversationScenario['empty'] =
      snapshot === null
        ? activeSessionId === null
          ? {
            title: '在工作区里动手做事',
            description: '读代码、改文件、跑受控命令；每一步都在本机，都能核对。',
            highlightsTitle: '它替你做的三件事',
            highlights: [
              '跨文件读代码，回答带路径与行号',
              '改文件只动该动的部分，先看 diff 再落盘',
              '命令和测试在受控范围里跑，需要授权先问你',
            ],
            hint: '还没有会话：在左侧新建一个，或换一个工作区。',
          }
          : { title: '正在打开会话', description: '正在读取这个会话的权威状态。' }
        : null

    // 快照里的轮次错误（不是消息行）也如实列出来：它们是"这一轮为什么失败"的依据。
    const visibleErrorIds = new Set(
      (snapshot?.messages ?? []).filter((message) => message.kind === 'error').map((message) => message.error.diagnostic_id),
    )
    const errors: MessageView[] = snapshot === null ? [] : (snapshot.errors ?? [])
      .filter((error) => !visibleErrorIds.has(error.diagnostic_id))
      .map((error) => ({ kind: 'error' as const, id: error.diagnostic_id, notice: mapError(error) }))

    const messages = snapshot === null ? [] : collapseDuplicateErrors([...mapMessages(snapshot, tail), ...errors])
      // 展开态在这里注入：适配层只给"默认紧凑"，被点过的行按界面状态覆盖。
      .map((message) =>
        message.kind === 'tool'
          ? { ...message, tool: { ...message.tool, expanded: expandedTools.has(message.id)
            || (message.tool.name === 'run_shell' && message.tool.status === 'running') } }
          : message,
      )

    // 安全阶段摘要（§6.7）：只表达"现在在做什么"，不暴露节点名或隐藏推理。
    const toolInFlight = [...messages].reverse().find((message) => message.kind === 'tool' && message.tool.status === 'running')
    // 当前轮的固定配置与下一轮选择分开：切换模型不改变正在运行的回答。
    const answerStreaming = snapshot?.current_answer_streaming === true
    const streamingTail = answerStreaming ? tail : null
    // Model-call start events currently arrive with completion. Only the snapshot's
    // running state, actual tool states and safe answer tail can describe activity.
    const activity = submit.isPending && !running && !awaitingApproval ? {
      id: `submitting-${activeSessionId ?? 'pending'}`, active: true, startedAt: null,
      label: '正在提交请求', detail: '正在等待服务端确认。',
    } : running || awaitingApproval ? {
      id: snapshot?.current_task?.run_id ?? activeSessionId ?? 'pending',
      active: running,
      startedAt: snapshot?.usage?.run_id === snapshot?.current_task?.run_id ? snapshot?.usage?.submitted_at ?? null : null,
      label: awaitingApproval ? '等待你确认'
        : toolInFlight?.kind === 'tool' ? `正在${toolLabel(toolInFlight.tool.name)}`
          : streamingTail !== null && streamingTail.text !== '' ? '正在接收回答'
            : streamingTail?.channel === 'reasoning' ? '正在思考' : '正在处理请求',
      // 未流式时**明说**回答会一次性出现（§19.4）：否则用户面对的是"长时间什么都没发生"，
      // 只能自己猜是不是卡住了。这一句只在确实没在等工具、也没在等审批时出现。
      detail: awaitingApproval ? '下方有一项操作需要你的决定，确认前不会执行。'
        : toolInFlight?.kind === 'tool' && toolInFlight.tool.target !== null ? toolInFlight.tool.target
          : snapshot?.current_answer_streaming === false
            ? '未启用流式：回答会在完成时一次性显示。'
            : '任务仍在运行，你可以继续写下一条消息的草稿。',
    } : null

    return {
      id: 'live',
      purpose: '',
      sessionId: activeSessionId,
      session: session ?? { title: '新会话', workspace: null, status: 'idle', modelLabel: '' },
      messages,
      activity,
      plan: snapshot === null ? null : mapPlan(snapshot.plan),
      stats: snapshot === null || running || awaitingApproval || (snapshot.turn_usages ?? []).some((turn) => turn.usage.run_id === snapshot.usage?.run_id)
        ? null : mapStats(snapshot),
      turnStats: snapshot === null ? [] : mapTurnStats(snapshot, messages, running || awaitingApproval),
      context: snapshot === null ? { usedTokens: null, windowTokens: null, scope: 'unknown' } : mapContext(snapshot),
      takeover: approvalView === null ? { kind: 'none' } : { kind: 'approval', approval: approvalView },
      settings: null,
      modelMenu,
      composer: {
        disabledReason:
          createSession.isPending
            ? '正在创建会话，请稍候。'
            : activeSessionId === null
              ? '先新建或打开一个会话，再发送消息。'
            : health.data?.model_configured === false
              ? '还没有可用的模型连接：点左下角「设置」加一条，保存后就能直接对话。'
              : null,
        phase: null,
        meterOpen,
      },
      empty: messages.length === 0 && !running && !awaitingApproval && !submit.isPending ? empty ?? {
        title: '在工作区里动手做事',
        description: '读代码、改文件、跑受控命令；每一步都在本机，都能核对。',
        highlightsTitle: '它替你做的三件事',
        highlights: [
          '跨文件读代码，回答带路径与行号',
          '改文件只动该动的部分，先看 diff 再落盘',
          '命令和测试在受控范围里跑，需要授权先问你',
        ],
        hint: currentMode === 'chat'
          ? '可以直接提问，也可以让我读取工作区文件并解释内容。'
          : '让我解释项目、定位问题或修改文件。需要确认的操作会先交给你决定。',
      } : null,
      // `isFetching`（不是 `isPending`）：没有会话时这个查询是**未启用**的，`isPending` 会长期为真，
      // 那会让"还没有会话"的空态被骨架盖住（实测）。
      loading:
        snapshot === null && activeSessionId !== null && snapshotQuery.isFetching
          ? { label: '正在读取会话', detail: '以服务端快照为准', rows: [70, 90, 55] }
          : null,
    }
  }, [
    activeSessionId,
    approvalView,
    awaitingApproval,
    createSession.isPending,
    currentMode,
    expandedTools,
    health.data?.model_configured,
    meterOpen,
    modelMenu,
    running,
    session,
    snapshot,
    snapshotQuery.isFetching,
    submit.isPending,
    tail,
  ])

  /**
   * 轨迹页签的数据来源与对话页签**完全相同**：同一份消息（工具行的展开态与它无关，不参与）
   * 加上会话级模型调用。两个页签因此不可能各说一套。
   */
  const trace = useMemo(
    () =>
      traceFromMessages(
        scenario.messages,
        snapshot === null ? [] : (snapshot.model_calls ?? []).map(mapCall),
        // 每轮统计（2026-10-16）：轨迹页与对话页用**同一份**锚点与数字（`mapTurnStats`），
        // 于是"这一轮花了多少 / 缓存命不命中 / 上下文用掉多少"两个页签不会各说一套。
        snapshot === null ? [] : mapTurnStats(snapshot, scenario.messages),
      ),
    [scenario.messages, snapshot],
  )

  const changeModel = (connectionId: string | null) => {
    if (snapshot === null || selectSessionModel.isPending) return
    const target = connectionId === null
      ? null
      : connections.data?.find((item) => item.connection_id === connectionId)
    if (connectionId !== null && target === undefined) {
      setModelError('这条模型连接已不在列表中，请刷新连接列表。')
      return
    }
    try {
      selectSessionModel.mutate({
        connectionId,
        expectation: selectionExpectation(snapshot),
        expectedVersion: target == null ? null : parseSelectionVersion(target.config_version),
      }, {
        onSuccess: () => {
          setModelError(null)
          setModelMenuOpen(false)
        },
        onError: (error) => {
          setModelError(error instanceof ApiError && error.status === 409
            ? '模型选择已被其他页面修改；请刷新会话与连接列表后重新选择。'
            : '模型选择未保存。请检查连接状态后重试。')
        },
      })
    } catch {
      setModelError('模型连接版本无效，请刷新后重试。')
    }
  }

  /**
   * 把正在跑的命令转入后台（2026-10-21）。
   *
   * 只发一个信号：后端让 `run_shell` 立刻返回"命令仍在后台"，模型据此继续下一步；
   * 返回 `false` = 这条命令已经结束（后端找不到还在等待的它），行上如实显示"已结束"。
   */
  const detachRunningCommand = async (commandId: string): Promise<boolean> => {
    if (activeSessionId === null) return false
    try {
      const result = await requestDetachCommand(activeSessionId, commandId)
      return result.detached
    } catch {
      return false
    }
  }

  /** 展开/收起一行工具结果：只改界面状态，不触发任何请求。 */
  const toggleTool = (messageId: string) => {
    setExpandedTools((current) => {
      const next = new Set(current)
      if (!next.delete(messageId)) next.add(messageId)
      return next
    })
  }

  /**
   * 把一张图交给服务端（2026-10-16），返回它的**引用**与本地预览地址。
   *
   * 走 base64 的 JSON（见 `uploadAttachment` 的注释）。`data:` 前缀只是传输封装——
   * 服务端按**魔数**自己判类型，页面声明的类型它不看（改名成 png 的 HTML 会被当场拒掉）。
   */
  const attachFile = async (file: File) => {
    if (activeSessionId === null) throw new Error('还没有打开会话')
    // 先压再传（2026-10-20）：附件会以 data: URL 进消息、随请求体发给模型，几 MB 的截图
    // 会被端点直接 400。压缩失败时抛出可执行的中文提示，Composer 原样显示。
    const prepared = await prepareImageForUpload(file)
    const saved = await uploadAttachment(activeSessionId, prepared.base64)
    return { id: saved.attachment_id, previewUrl: URL.createObjectURL(prepared.blob) }
  }

  const send = (attachmentIds: readonly string[] = []): boolean => {
    // 只贴图不写字也是合法提交（2026-10-16）：`text` 与 `attachments` 至少有一个。
    // 返回 false = **这次提交没有被接受**（没有发请求）。调用方据此保留待发图片，
    // 不能先清队列再问——2026-10-20 实测过"文字还在、图片没了"。
    if (activeSessionId === null || (draft.trim() === '' && attachmentIds.length === 0) || running || awaitingApproval || interrupted || submit.isPending || createSession.isPending || setSessionMode.isPending) return false
    const text = draft
    // 先清草稿：提交的幂等键重试由用户发起（见 queries.ts），草稿不该在失败后被吞掉，
    // 但也不该在成功后又发一遍——所以这里清掉，失败时用户可以重新输入。
    setDraft(activeSessionId, '')
    submit.mutate({ text, attachments: attachmentIds }, {
      onSuccess: () => { setFollowup(null); setFocusTurn(null) },
      onError: () => {
        // 服务端可能先进入待审批，或另一页面抢先提交。只有用户尚未写新草稿时才恢复原文；
        // 绝不拿旧请求覆盖用户在执行期间刚写的内容。
        if ((useUiStore.getState().drafts[activeSessionId] ?? '') === '') setDraft(activeSessionId, text)
      },
    })
    return true
  }

  /**
   * 提交一次审批决定。
   *
   * 两件事刻意分开做：**提交后立即锁定**由 `approvalDecision.isPending` 驱动（按钮立刻禁用，
   * 不等任何本地推断），而**最终状态以后端为准**——`useResolveApproval` 无论成败都会重取快照，
   * 所以"批准成功 → 卡片变已批准"和"决定已失效 → 卡片变已失效"都来自服务端（§15.8）。
   */
  const decideApproval = (decision: 'approve' | 'reject', confirmRecursive = false) => {
    // 卡片只在快照存在时渲染，因此这里的会话 id 一定有效；仍显式挡住并发的第二次点击。
    if (activeSessionId === null || approvalView === null || approvalDecision.isPending) return
    approvalDecision.resolve(approvalView.approvalId, decision, confirmRecursive)
  }

  /** 审批失败按错误码措辞：过期与忙碌要给出不同、且可执行的下一步。 */
  const approvalErrorMessage =
    approvalDecision.error === null
      ? null
      : approvalDecision.error instanceof ApiError
        ? approvalDecision.error.code === 'APPROVAL_EXPIRED'
          ? '这次审批已失效，未执行任何操作。请重新发起请求。'
          : approvalDecision.error.code === 'SESSION_BUSY'
            ? '会话正忙，这次审批决定没有提交。稍后重试即可，同一个决定不会重复执行。'
            : `${approvalDecision.error.message}${approvalDecision.error.hint === null ? '' : ` ${approvalDecision.error.hint}`}`
        : '审批决定未能提交。请刷新会话核对状态后再试。'

  // 选已有工作区、弹出目录选择器和登记新目录共用这处就地错误提示；成功/重试时清除旧错误。
  // 取消目录选择器是正常响应（path=null），不显示错误。

  const refreshCatalog = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['sessions'] }),
      queryClient.invalidateQueries({ queryKey: queryKeys.workspaces }),
      queryClient.invalidateQueries({ queryKey: ['snapshot'] }),
    ])
  }

  const openSelected = (selected: Awaited<ReturnType<typeof selectSession>>) => {
    openSession(selected.session_id)
    setWorkspace(workspaces.data?.some((item) => item.workspace_id === selected.workspace.workspace_id)
      ? selected.workspace.workspace_id : null)
  }

  const displayScenario = selectedFollowup !== null && !running && !submit.isPending && snapshot?.snapshot_id === selectedFollowup.baseSnapshotId ? {
    ...scenario,
    messages: mapMessages(selectedFollowup.context).map((message) => message.kind === 'tool' ?
      { ...message, tool: { ...message.tool, expanded: expandedTools.has(message.id) } } : message),
    activity: null, empty: null, loading: null, stats: null,
    turnStats: mapTurnStats(selectedFollowup.context, mapMessages(selectedFollowup.context), false),
  } : scenario
  const composer = <Composer
    mode={currentMode}
    session={session ?? { title: '新会话', workspace: null, status: 'idle', modelLabel: '' }}
    plan={scenario.plan}
    meter={scenario.context}
    takeover={scenario.takeover}
    modelMenu={scenario.modelMenu}
    modelMenuActions={modelMenu === null ? undefined : {
      busy: selectSessionModel.isPending,
      onSelect: changeModel,
      onFollowDefault: () => changeModel(null),
      onManage: () => {
        setModelMenuOpen(false)
        setSettingsOpen(true)
      },
    }}
    state={scenario.composer}
    // §17.4：这一轮是不是"被停下来的"由服务端说（`final_result.state`），页面不自己推。
    stopped={snapshot?.final_result?.state === 'cancelled'}
    // 审批按钮的真实接线：提交后 isPending 立即为真 → 两个按钮立刻锁定，
    // 之后卡片内容完全由重取到的快照决定（后端结果是权威）。
    approvalActions={{
      busy: approvalDecision.isPending,
      onApprove: (confirmRecursive) => decideApproval('approve', confirmRecursive),
      onReject: () => decideApproval('reject'),
    }}
    actions={{
      draft,
      onDraftChange: (text) => {
        if (activeSessionId !== null) setDraft(activeSessionId, text)
      },
      onSend: send,
      // 图片入口（2026-10-16）：粘贴与选图都走这一条（Composer 负责排队，LiveApp 只负责上传）。
      onAttachFile: attachFile,
      onToggleModelMenu: () => {
        setModelError(null)
        setModelMenuOpen((open) => !open)
      },
      onCloseModelMenu: () => setModelMenuOpen(false),
      onToggleMeter: () => setMeterOpen((open) => !open),
      onSelectMode: (mode) => {
        if (activeSessionId === null || mode === currentMode || setSessionMode.isPending || running || awaitingApproval || interrupted || submit.isPending) return
        setModeError(null)
        const sessionId = activeSessionId
        setSessionMode.mutate({ sessionId, mode, expectedMode: currentMode }, {
          onError: (error) => setModeError({ sessionId, message: error instanceof ApiError
            ? error.message : '模式切换未保存，请检查连接后重试。' }),
        })
      },
      modeDisabled: activeSessionId === null || running || awaitingApproval || interrupted || submit.isPending || setSessionMode.isPending,
      // 停止（§17.4）：只在**真的有在跑的轮次**时出现；等审批时没有在飞的调用，取消本就立即生效，
      // 但那一轮由审批卡接管，不给停止按钮（避免两个入口表达同一次决定）。
      onStop: () => {
        if (activeSessionId === null || !running) return
        cancelTask.mutate({ sessionId: activeSessionId, reason: '用户按下了停止' })
      },
      canStop: activeSessionId !== null && running && !awaitingApproval && !interrupted,
      stopping: cancelTask.isPending,
      // 任务进行中**允许继续写**，但不让发（T30 裁定）：草稿留在输入框里等这一轮结束。
      canSend: activeSessionId !== null && draft.trim() !== '' && !running && !awaitingApproval && !interrupted && !submit.isPending && !cancelTask.isPending && !createSession.isPending && !setSessionMode.isPending,
    }}
  />

  return (
    <div className={styles.app} data-scenario="live">
      <Sidebar
        workspaces={sidebar}
        collapsed={sidebarCollapsed}
        addWorkspace={{ pending: pickWorkspace.isPending, error: workspaceActionError }}
        management={{
          renameSession: async (id, title) => { await renameSession(id, title); await refreshCatalog() },
          renameWorkspace: async (id, title) => { await renameWorkspace(id, title); await refreshCatalog() },
          removeWorkspace: async (id) => { await removeWorkspace(id); await refreshCatalog() },
          archiveSession: async (id) => {
            await archiveSession(id)
            if (id === useUiStore.getState().activeSessionId) {
              const remaining = await listSessions()
              queryClient.setQueryData(queryKeys.sessions(null), remaining)
              const next = remaining.find((item) => item.is_active)
              if (next !== undefined) openSelected(next)
              else { openSession(null); setWorkspace(null) }
            }
            await refreshCatalog()
          },
          restoreSession: async (id) => { await restoreSession(id); await refreshCatalog() },
          archivedOpen, onToggleArchived: () => setArchivedOpen(!archivedOpen),
          archivedSessions: (archivedSessions.data ?? []).map((item) => ({ id: item.session_id, title: item.title })),
          archivedLoading: archivedSessions.isLoading,
          archivedError: archivedSessions.isError ? '归档列表暂时无法读取，收起后重新打开可重试。' : null,
        }}
        actions={{
          onSelectWorkspace: (id) => {
            setDataCenterOpen(false)
            setWorkspaceActionError(null)
            selectWorkspace.mutate({ workspace_id: id }, {
              onSuccess: (selected) => {
                setWorkspace(selected.workspace?.workspace_id ?? id)
                openSession(selected.session_id)
              },
              onError: (error) => {
                setWorkspaceActionError(error instanceof ApiError
                  ? `${error.message}${error.hint === null ? '' : ` ${error.hint}`}`
                  : '没能切换工作区。请重试。')
              },
            })
          },
          /**
           * 新增工作区 = 两步，刻意不合成一次调用。
           *
           * 第一步只取路径（取消是零副作用的成功响应），第二步才登记——登记完全复用既有的
           * `selectWorkspace`，于是"路径非法 / 目录不存在"的错误码与幂等语义一份都不用重写。
           * 新增后**立刻选中它并打开它的会话**：用户刚选完目录，期待看到的就是它。
           */
          onAddWorkspace: () => {
            if (pickWorkspace.isPending) return
            setWorkspaceActionError(null)
            pickWorkspace.mutate(undefined, {
              onSuccess: ({ path }) => {
                if (path === null) return
                selectWorkspace.mutate({ path }, {
                  onSuccess: (created) => {
                    setWorkspaceActionError(null)
                    openSession(created.session_id)
                    setWorkspace(created.workspace?.workspace_id ?? null)
                  },
                  onError: (error) => {
                    setWorkspaceActionError(error instanceof ApiError
                      ? `${error.message}${error.hint === null ? '' : ` ${error.hint}`}`
                      : '没能添加这个工作区。请重新选择目录。')
                  },
                })
              },
              onError: (error) => {
                setWorkspaceActionError(error instanceof ApiError
                  ? `${error.message}${error.hint === null ? '' : ` ${error.hint}`}`
                  : '没能打开目录选择器。重试一次即可。')
              },
            })
          },
          onSelectSession: (id) => {
            setDataCenterOpen(false)
            setWorkspaceActionError(null)
            void selectSession(id).then(async (selected) => {
              openSelected(selected)
              await queryClient.invalidateQueries({ queryKey: ['sessions'] })
            }).catch((error: unknown) => setWorkspaceActionError(error instanceof ApiError
              ? error.message : '没能打开会话，请重试。'))
          },
          onNewSession: () => {
            setDataCenterOpen(false)
            if (createSession.isPending) return
            setWorkspaceActionError(null)
            createSession.mutate({ workspaceId, mode: currentMode }, {
              onSuccess: (created) => {
                openSession(created.session_id)
                setTab('conversation')
                setWorkspace(created.workspace?.workspace_id ?? null)
              },
              onError: (error) => setWorkspaceActionError(error instanceof ApiError
                ? error.message : '没能新建会话，请重试。'),
            })
          },
          onOpenSettings: () => setSettingsOpen(true),
          // 六个整页入口互斥：同时叠两个会让用户以为"这里也能看那里"。
          onOpenDataCenter: () => {
            setSettingsOpen(false); setCapabilitiesOpen(false); setCronOpen(false)
            setArtifactsOpen(false); setDataCenterOpen(true)
          },
          onOpenTasks: () => {
            setSettingsOpen(false); setDataCenterOpen(false); setCapabilitiesOpen(false)
            setCronOpen(false); setArtifactsOpen(false); setTasksOpen(true)
          },
          onOpenCapabilities: () => {
            setSettingsOpen(false); setDataCenterOpen(false); setTasksOpen(false)
            setCronOpen(false); setArtifactsOpen(false); setCapabilitiesOpen(true)
          },
          onOpenCron: () => {
            setSettingsOpen(false); setDataCenterOpen(false); setTasksOpen(false)
            setCapabilitiesOpen(false); setArtifactsOpen(false); setCronOpen(true)
          },
          onOpenArtifacts: () => {
            setSettingsOpen(false); setDataCenterOpen(false); setTasksOpen(false)
            setCapabilitiesOpen(false); setCronOpen(false); setArtifactsOpen(true)
          },
          pendingTasks: tasks.data?.pending_count ?? 0,
          onToggleCollapse: () => setCollapsedPref(!sidebarCollapsed),
        }}
      />
      <main className={styles.main}>
        {dataCenterOpen ? <DataCenter workspaces={workspaces.data ?? []} initialWorkspaceId={workspaceId}
          running={running || awaitingApproval} onClose={() => setDataCenterOpen(false)} />
          : capabilitiesOpen ? <CapabilitiesCenter onClose={() => setCapabilitiesOpen(false)} />
            : cronOpen ? <CronPanel onOpenSession={(id) => {
              void selectSession(id).then((selected) => {
                setCronOpen(false)
                openSelected(selected)
              }).catch(() => { /* 会话可能已被清理，保持停留在任务页 */ })
            }} />
              : artifactsOpen ? <ArtifactsPanel onClose={() => setArtifactsOpen(false)}
                onOpenSession={(id) => {
                  void selectSession(id).then((selected) => {
                    setArtifactsOpen(false)
                    openSelected(selected)
                  }).catch(() => { /* 来源会话可能已被清理 */ })
                }} /> : <>
        <TopBar
          title={session?.title ?? '新会话'}
          workspace={session?.workspace ?? null}
          activeTab={tab}
          onSelectTab={setTab}
          onOpenMap={() => setTab('map')}
          onOpenSettings={() => setSettingsOpen(true)}
          sideChatParent={sideChatParentTitle}
          onPromoteSideChat={() => {
            if (activeSessionId === null) return
            void promoteSideChat(activeSessionId)
              .then(() => queryClient.invalidateQueries({ queryKey: queryKeys.sessions(workspaceId) }))
              .catch((error: unknown) => setWorkspaceActionError(
                error instanceof ApiError ? error.message : '没能提升，请重试。'))
          }}
          panelOpen={panelOpen}
          onTogglePanel={() => setPanelOpenPersisted(!panelOpen)}
        />
        {selectedFollowup === null ? snapshot?.branch_id && snapshot.branch_id !== 'main' ?
          <Notice label="分支提示" scope={`${activeSessionId}:${snapshot.branch_id}`}>当前分支：{snapshot.branch_title} · 新提问会接在此分支末尾。</Notice> : null :
          <Notice label="历史追问提示" scope={`${activeSessionId}:${selectedFollowup.turnId}`}>基于第{selectedFollowup.ordinal}轮继续提问：{selectedFollowup.question.slice(0, 100)}。
            上下文包含会话开头到本轮，原后续问答保留。
            <button type="button" disabled={running || awaitingApproval || submit.isPending}
              onClick={() => setFollowup(null)}>取消历史追问</button></Notice>}
        {stream.reconnecting ? (
          // 断线必须可见（v0.8 T72；§19.2）：连不上时如实说明，并写明"不会自动重发请求"。
          // 一轮正常收尾后的 500ms 重订阅不算断线，所以不会在这里闪。
          <Notice label="重连提示" scope={`${activeSessionId}:reconnecting`} role="status">
            连接已中断，正在重连。会话在服务端继续推进；恢复后会以最新状态为准，不会自动重发请求。
          </Notice>
        ) : null}
        {stream.notices.length === 0 ? null : (
          // 传输层提示（超限/空闲超时/无法解析的帧）如实显示：它们不代表数据损坏，但用户有权知道。
          <Notice label="连接提示" scope={`${activeSessionId}:${stream.notices.join(",")}`} role="status">
            {stream.notices.includes('snapshot_refresh_failed')
              ? '暂时没能更新会话状态，正在恢复连接。请等待状态恢复后再操作。'
              : '实时连接需要重新同步，正在重新读取会话状态。不会自动重发请求。'}
          </Notice>
        )}
        {approvalErrorMessage === null ? null : (
          // 决定没提交成功时如实说明，并指向"重取快照"这条唯一可靠的核对途径（§15.8）。
          <Notice label="审批错误提示" scope={`${activeSessionId}:${approvalErrorMessage}`} role="alert">
            {approvalErrorMessage}{' '}
            <button
              type="button"
              onClick={() => {
                if (activeSessionId !== null) {
                  void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(activeSessionId) })
                }
              }}
            >
              刷新状态
            </button>
          </Notice>
        )}
        {cancelTask.isError ? <Notice key={activeSessionId} label="停止错误提示" scope={cancelTask.error} role="alert">
          {cancelTask.error instanceof ApiError ? cancelTask.error.message : '停止请求未能确认，请刷新状态后再次停止。'}
          <button type="button" onClick={() => {
            if (activeSessionId !== null) void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(activeSessionId) })
          }}>刷新状态</button>
        </Notice> : null}
        {submit.isError ? (
          <Notice key={activeSessionId} label="提交错误提示" scope={submit.error} role="alert">
            {submit.error instanceof ApiError && submit.error.code !== null && submit.error.code !== 'RECOVERY_REQUIRED'
              ? `${submit.error.message}${submit.error.hint === null ? '' : ` ${submit.error.hint}`}`
              : '这次提交的执行结果尚不能确认。请先刷新会话核对状态；若可能涉及工具副作用，不要直接重复执行。'}
          </Notice>
        ) : null}
        {interrupted ? (
          // 2026-10-23 实机反馈「点了勾没有任何响应」：原来复用了通用的 `.notice`（纯块级、
          // 没有布局），那个 `<button>` 是**裸原生按钮**，和正文字长得一样；勾选后它只是
          // 从 disabled 变成可点，外观毫无变化，用户当然以为没反应。
          <div className={styles.recovery} role="alert">
            <p className={styles.recoveryLead}>本轮执行已中断，工具执行结果需要核对。刷新不会重跑工具。</p>
            <div className={styles.recoveryRow}>
              <label className={styles.recoveryCheck}>
                <input type="checkbox" checked={recoveryConfirm}
                  onChange={(event) => setRecoveryConfirm(event.target.checked)} />
                我已查看本轮记录并核对相关文件或命令结果
              </label>
              <button type="button" className={styles.recoveryButton}
                disabled={!recoveryConfirm || endInterrupted.isPending}
                onClick={() => {
                  if (snapshot === null || snapshot.interrupted_request_id == null) return
                  endInterrupted.mutate({ sessionId: snapshot.session_id, requestId: snapshot.interrupted_request_id,
                    snapshotId: snapshot.snapshot_id }, { onSuccess: () => {
                      setRecoveryConfirm(false)
                      submit.reset()
                      approvalDecision.reset()
                    } })
                }}>{endInterrupted.isPending ? '正在结束…' : '结束中断任务'}</button>
            </div>
            {endInterrupted.isError ? <p>未能结束本轮，请刷新状态后重新核对。</p> : null}
          </div>
        ) : null}
        {modeError?.sessionId === activeSessionId ? <Notice label="模式错误提示" scope={`${activeSessionId}:${modeError.message}`} role="alert">{modeError.message}</Notice> : null}
        {createSession.isError ? (
          <Notice label="新建会话错误提示" scope={createSession.error} role="alert">没能新建会话。请检查工作区状态后重试。</Notice>
        ) : null}
        {failedWithoutDetail ? (
          <Notice label="执行失败提示" scope={`${activeSessionId}:${snapshot?.snapshot_id}`} role="alert">
            这轮在给出最终回答前就结束了。可能是本轮达到了调用上限，或模型调用失败；
            请查看本轮记录后再发起新请求。
          </Notice>
        ) : null}
        {modelError === null ? null : (
          <Notice label="模型错误提示" scope={`${activeSessionId}:${modelError}`} role="alert">
            {modelError}{' '}
            <button type="button" onClick={() => {
              void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(activeSessionId ?? '') })
              void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections })
              setModelError(null)
            }}>刷新状态</button>
          </Notice>
        )}
        {tab === 'map' && activeSessionId !== null ? (
          <ConversationMapPanel key={activeSessionId} sessionId={activeSessionId} scenario={scenario}
            errorActions={errorActions}
            snapshotId={snapshot?.snapshot_id ?? null} busy={running || awaitingApproval || submit.isPending}
            onContinue={(node, context) => {
              if (snapshot === null || useUiStore.getState().activeSessionId !== activeSessionId) return
              setFollowup({ sessionId: activeSessionId, turnId: node.id, question: node.question,
                ordinal: node.ordinal, context, baseSnapshotId: snapshot.snapshot_id, branchId: snapshot.branch_id ?? 'main' })
              setFocusTurn(null)
              setMapChat({ sessionId: activeSessionId, turnId: node.id })
            }}
            followupTurnId={mapChat?.sessionId === activeSessionId ? mapChat.turnId : null}
            onCloseFollowup={() => { setMapChat(null); setFollowup(null) }}
            followupContent={<><Conversation scenario={displayScenario} autoFollow onToggleTool={toggleTool} errorActions={errorActions} />
              {snapshot?.workflow ? <WorkflowStatus view={mapWorkflow(snapshot.workflow)} /> : null}{composer}</>}
            onLocate={(turnId) => {
              setMapChat(null); setFollowup(null); setFocusTurn({ sessionId: activeSessionId, turnId }); setTab('conversation')
            }}
            onBranchChanged={() => { setMapChat(null); setFollowup(null); setFocusTurn(null) }} />
        ) : tab === 'trace' ? (
          <TraceView trace={trace} />
        ) : (
          <Conversation scenario={displayScenario} autoFollow onToggleTool={toggleTool} errorActions={errorActions}
            onDetachCommand={detachRunningCommand}
            focusTurnId={focusTurn?.sessionId === activeSessionId ? focusTurn.turnId : null}
            // 每轮末尾的改动摘要（2026-10-21）：它是对话流里的一条，不占固定空间。
            renderTurnChanges={(turnId) => (
              <TurnChanges
                sessionId={activeSessionId}
                turnId={turnId}
                selectedPath={changesPath}
                // 点改动条里的文件 → 打开右栏并直接展开它的 diff（2026-10-22 恢复原来的行为）。
                onSelect={(path) => {
                  setChangesPath(path)
                  setPanelFocus({ tab: 'changes', path })
                  setPanelOpenPersisted(true)
                }}
              />
            )}
            // 就地编辑（2026-10-21）：**不回填底部输入框**，直接以"上一轮之后"重新提交。
            // 返回 false = 这次提交没被接受（一轮在跑 / 待审批 / 正在提交），气泡留在编辑态。
            onEditMessage={(parentTurnId, text, attachments) => {
              if (activeSessionId === null) return false
              if (running || awaitingApproval || interrupted || submit.isPending || createSession.isPending || setSessionMode.isPending) return false
              setFollowup(null)
              submit.mutate({
                text,
                attachments,
                // 行内编辑必须显式给这一轮的父：hook 级 context 里的 parent 是渲染期算的，
                // 而编辑点是运行期才知道的。
                context: {
                  parent_turn_id: parentTurnId,
                  expected_branch_id: snapshot?.branch_id,
                  expected_snapshot_id: snapshot?.snapshot_id,
                },
              }, { onSuccess: () => { setFollowup(null); setFocusTurn(null) } })
              return true
            }} />
        )}
        {tab === 'conversation' && snapshot?.workflow ? <WorkflowStatus view={mapWorkflow(snapshot.workflow)} /> : null}
        {/* 输入区属于对话页。轨迹页是只读的，**不该带输入框**（2026-10-23 实机反馈）。
            唯一的例外是等审批：审批卡挂在 `Composer` 的 takeover 里，没有第二个入口，
            这时候藏掉输入区等于让人没法批准，所以轨迹页在这期间保留。
            地图页的例外更窄：抽屉自己已渲染一份输入与审批，只有抽屉没托着当前会话时才补一个。 */}
        {tab === 'conversation'
          || (tab === 'trace' && awaitingApproval)
          || (tab === 'map' && awaitingApproval && mapChat?.sessionId !== activeSessionId)
          ? composer : null}
        </>}
        {tasksOpen ? (
          <TasksSection
            workspaceId={workspaceId}
            onOpenSession={(sessionId) => {
              // 恢复入口：三类判定都指向"去会话里处理"（§17.6），这里只负责打开它。
              openSession(sessionId)
              setTasksOpen(false)
              setTab('conversation')
            }}
            onClose={() => setTasksOpen(false)}
            onCloseReview={(taskId, outcome) => {
              // 结案成功后刷任务索引：侧栏那行「需人工核对」随之消失（§17.1 唯一例外）。
              closeTaskReview.mutate({ jobId: taskId, outcome })
            }}
          />
        ) : null}
        {settingsOpen ? <ModelSettingsDialog onClose={() => {
          setSettingsOpen(false)
          void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections })
          void queryClient.invalidateQueries({ queryKey: queryKeys.health })
          if (activeSessionId !== null) void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(activeSessionId) })
        }} /> : null}
      </main>
      {/* 右栏工作面板（§21.6）：与中栏并列，只读。 */}
      {panelOpen ? (
        // 换会话就整体重建：右栏状态按会话存，切过去是「新会话自己的」那套页签，
        // 而不是把上一个会话的页签带过来（那会指向不存在的工作区路径）。
        <PanelShell
          key={activeSessionId ?? 'none'}
          workspaceId={workspaceId}
          sessionId={activeSessionId}
          trace={trace}
          onOpenStep={(turnId) => {
            // 与「会话地图」的"定位到这一轮"走同一条路径：切回对话并让该轮进入视野。
            if (activeSessionId === null) return
            setMapChat(null)
            setFollowup(null)
            setFocusTurn({ sessionId: activeSessionId, turnId })
            setTab('conversation')
          }}
          focus={panelFocus}
          onClose={() => { setPanelOpenPersisted(false); setPanelFocus(null) }}
        />
      ) : null}
      {paletteOpen ? (
        <CommandPalette
          onClose={() => setPaletteOpen(false)}
          onOpenSession={(id) => {
            void selectSession(id).then(async (selected) => {
              openSelected(selected)
              await queryClient.invalidateQueries({ queryKey: ['sessions'] })
            }).catch((error: unknown) => setWorkspaceActionError(
              error instanceof ApiError ? error.message : '没能打开会话，请重试。'))
          }}
          actions={[
            { id: 'new-session', label: '新建会话', hint: '动作', onSelect: () => createSession.mutate({ workspaceId, mode: currentMode }, {
              onSuccess: (created) => openSession(created.session_id),
            }) },
            { id: 'settings', label: '打开设置', hint: '动作', onSelect: () => setSettingsOpen(true) },
            { id: 'capabilities', label: '打开能力中心', hint: '动作', onSelect: () => setCapabilitiesOpen(true) },
            { id: 'data-center', label: '打开数据中台', hint: '动作', onSelect: () => setDataCenterOpen(true) },
            { id: 'tasks', label: '打开任务中心', hint: '动作', onSelect: () => setTasksOpen(true) },
            { id: 'trace', label: '查看本轮轨迹', hint: '动作', onSelect: () => setTab('trace') },
          ]}
        />
      ) : null}
    </div>
  )
}
