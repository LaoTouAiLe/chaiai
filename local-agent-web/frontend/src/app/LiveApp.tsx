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

import { Composer } from '../components/Composer'
import { Sidebar } from '../components/Sidebar'
import { TopBar } from '../components/TopBar'
import { Conversation } from '../features/conversation/Conversation'
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
import { mapSidebar } from '../features/sidebar/adapters'
import { ModelSettingsDialog } from '../features/settings/ModelSettingsDialog'
import { parseSelectionVersion, selectionExpectation, toModelMenu } from '../features/settings/modelMenuAdapter'
import { traceFromMessages } from '../features/trace/adapters'
import { TraceView } from '../features/trace/TraceView'
import { ConversationMapPanel } from '../features/conversation-map/ConversationMapPanel'
import { DataCenter } from '../features/data-center/DataCenter'
import type { components } from '../api/schema.gen'
import { ApiError, archiveSession, listSessions, removeWorkspace, renameSession, renameWorkspace, restoreSession, selectSession } from '../api/client'
import { queryKeys, useHealth, useModelConnections, useSessions, useSnapshot, useSubmitMessage, useWorkspaces, useCreateSession, useSelectWorkspace, useSelectSessionModel, useResolveApproval, usePickWorkspaceDirectory, useSetSessionMode, useEndInterruptedRequest } from '../api/queries'
import { useSessionStream } from '../api/useSessionStream'
import { useStreamTail } from '../stores/stream'
import { useUiStore } from '../stores/ui'
import type { ConversationScenario, MessageView } from '../fixtures/types'
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
  const [focusTurn, setFocusTurn] = useState<{ sessionId: string; turnId: string } | null>(null)
  // 工具行的展开态：**纯界面状态**，不来自快照。T32 的结构化 diff 就在展开后才可见
  // （`ToolRow` 收起时只有一行摘要），所以真会话必须能让用户按需展开。
  const [expandedTools, setExpandedTools] = useState<ReadonlySet<string>>(() => new Set())
  const queryClient = useQueryClient()

  const health = useHealth()
  const workspaces = useWorkspaces()
  const sessions = useSessions(null)
  const archivedSessions = useArchivedSessions(archivedOpen)
  const snapshotQuery = useSnapshot(activeSessionId)
  const connections = useModelConnections()
  const createSession = useCreateSession()
  const setSessionMode = useSetSessionMode()
  const endInterrupted = useEndInterruptedRequest()
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
  const snapshot = snapshotQuery.data ?? null
  const selectedFollowup = followup?.sessionId === activeSessionId ? followup : null
  const submit = useSubmitMessage(activeSessionId, {
    parent_turn_id: selectedFollowup?.turnId,
    expected_branch_id: selectedFollowup?.branchId ?? snapshot?.branch_id,
    expected_snapshot_id: selectedFollowup?.baseSnapshotId ?? snapshot?.snapshot_id,
  })
  const currentMode = snapshot?.mode ?? sessionList.find((item) => item.session_id === activeSessionId)?.mode ?? 'agent'

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
      }),
    [activeSessionId, modelLabel, sessionList, snapshot, workspaces.data],
  )

  const draft = activeSessionId === null ? '' : (drafts[activeSessionId] ?? '')
  const session = snapshot === null ? null : mapSession(snapshot, modelLabel)
  const running = session?.status === 'running'
  const awaitingApproval = session?.status === 'awaiting-approval'
  const interrupted = snapshot?.interrupted_request_id != null
  useEffect(() => { setRecoveryConfirm(false) }, [activeSessionId, snapshot?.snapshot_id, snapshot?.interrupted_request_id])
  const failedWithoutDetail = snapshot?.session_state === 'failed' && (snapshot.errors ?? []).length === 0

  // 审批卡直接由快照派生：**刷新恢复不需要任何额外机制**——快照本来就由 Checkpoint 投影，
  // 挂起的审批只要还在后端就会重新出现在这里（T32 停止条件之一）。
  const approvalView = snapshot?.approval == null ? null : mapApproval(snapshot.approval)

  const scenario: ConversationScenario = useMemo(() => {
    const empty: ConversationScenario['empty'] =
      snapshot === null
        ? {
          title: activeSessionId === null ? '还没有会话' : '正在打开会话',
          description:
            activeSessionId === null
              ? '在左侧新建一个会话，或者换一个工作区。'
              : '正在读取这个会话的权威状态。',
        }
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
          ? { ...message, tool: { ...message.tool, expanded: expandedTools.has(message.id) } }
          : message,
      )

    // 安全阶段摘要（§6.7）：只表达"现在在做什么"，不暴露节点名或隐藏推理。
    const toolInFlight = [...messages].reverse().find((message) => message.kind === 'tool' && message.tool.status === 'running')
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
          : tail !== null && tail.text !== '' ? '正在接收回答'
            : tail?.channel === 'reasoning' ? '正在思考' : '正在处理请求',
      detail: awaitingApproval ? '下方有一项操作需要你的决定，确认前不会执行。'
        : toolInFlight?.kind === 'tool' && toolInFlight.tool.target !== null ? toolInFlight.tool.target
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
        title: '开始一个新会话',
        description: currentMode === 'chat'
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

  /** 展开/收起一行工具结果：只改界面状态，不触发任何请求。 */
  const toggleTool = (messageId: string) => {
    setExpandedTools((current) => {
      const next = new Set(current)
      if (!next.delete(messageId)) next.add(messageId)
      return next
    })
  }

  const send = () => {
    if (activeSessionId === null || draft.trim() === '' || running || awaitingApproval || interrupted || submit.isPending || createSession.isPending || setSessionMode.isPending) return
    const text = draft
    // 先清草稿：提交的幂等键重试由用户发起（见 queries.ts），草稿不该在失败后被吞掉，
    // 但也不该在成功后又发一遍——所以这里清掉，失败时用户可以重新输入。
    setDraft(activeSessionId, '')
    submit.mutate(text, {
      onSuccess: () => { setFollowup(null); setFocusTurn(null) },
      onError: () => {
        // 服务端可能先进入待审批，或另一页面抢先提交。只有用户尚未写新草稿时才恢复原文；
        // 绝不拿旧请求覆盖用户在执行期间刚写的内容。
        if ((useUiStore.getState().drafts[activeSessionId] ?? '') === '') setDraft(activeSessionId, text)
      },
    })
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
          onOpenDataCenter: () => { setSettingsOpen(false); setDataCenterOpen(true) },
          onToggleCollapse: () => setCollapsedPref(!sidebarCollapsed),
        }}
      />
      <main className={styles.main}>
        {dataCenterOpen ? <DataCenter workspaces={workspaces.data ?? []} initialWorkspaceId={workspaceId}
          running={running || awaitingApproval} onClose={() => setDataCenterOpen(false)} /> : <>
        <TopBar
          title={session?.title ?? '新会话'}
          workspace={session?.workspace ?? null}
          activeTab={tab}
          onSelectTab={setTab}
          onOpenMap={() => setTab('map')}
        />
        {selectedFollowup === null ? snapshot?.branch_id && snapshot.branch_id !== 'main' ?
          <p className={styles.notice}>当前分支：{snapshot.branch_title} · 新提问会接在此分支末尾。</p> : null :
          <p className={styles.notice}>基于第{selectedFollowup.ordinal}轮继续提问：{selectedFollowup.question.slice(0, 100)}。
            上下文包含会话开头到本轮，原后续问答保留。
            <button type="button" disabled={running || awaitingApproval || submit.isPending}
              onClick={() => setFollowup(null)}>取消历史追问</button></p>}
        {stream.notices.length === 0 ? null : (
          // 传输层提示（超限/空闲超时/无法解析的帧）如实显示：它们不代表数据损坏，但用户有权知道。
          <p className={styles.notice} role="status">
            {stream.notices.includes('snapshot_refresh_failed')
              ? '暂时没能更新会话状态，正在恢复连接。请等待状态恢复后再操作。'
              : '实时连接需要重新同步，正在重新读取会话状态。不会自动重发请求。'}
          </p>
        )}
        {approvalErrorMessage === null ? null : (
          // 决定没提交成功时如实说明，并指向"重取快照"这条唯一可靠的核对途径（§15.8）。
          <p className={styles.notice} role="alert">
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
          </p>
        )}
        {submit.isError ? (
          <p className={styles.notice} role="alert">
            {submit.error instanceof ApiError && submit.error.code !== null && submit.error.code !== 'RECOVERY_REQUIRED'
              ? `${submit.error.message}${submit.error.hint === null ? '' : ` ${submit.error.hint}`}`
              : '这次提交的执行结果尚不能确认。请先刷新会话核对状态；若可能涉及工具副作用，不要直接重复执行。'}
          </p>
        ) : null}
        {interrupted ? <div className={styles.notice} role="alert">
          本轮执行已中断，工具执行结果需要核对。刷新不会重跑工具。
          <label><input type="checkbox" checked={recoveryConfirm} onChange={(event) => setRecoveryConfirm(event.target.checked)} />
            我已查看本轮记录并核对相关文件或命令结果</label>
          <button type="button" disabled={!recoveryConfirm || endInterrupted.isPending}
            onClick={() => {
              if (snapshot === null || snapshot.interrupted_request_id == null) return
              endInterrupted.mutate({ sessionId: snapshot.session_id, requestId: snapshot.interrupted_request_id,
                snapshotId: snapshot.snapshot_id }, { onSuccess: () => {
                  setRecoveryConfirm(false)
                  submit.reset()
                  approvalDecision.reset()
                } })
            }}>结束中断任务</button>
          {endInterrupted.isError ? <p>未能结束本轮，请刷新状态后重新核对。</p> : null}
        </div> : null}
        {modeError?.sessionId === activeSessionId ? <p className={styles.notice} role="alert">{modeError.message}</p> : null}
        {createSession.isError ? (
          <p className={styles.notice} role="alert">没能新建会话。请检查工作区状态后重试。</p>
        ) : null}
        {failedWithoutDetail ? (
          <p className={styles.notice} role="alert">
            这轮在给出最终回答前就结束了。可能是本轮达到了调用上限，或模型调用失败；
            请查看本轮记录后再发起新请求。
          </p>
        ) : null}
        {modelError === null ? null : (
          <p className={styles.notice} role="alert">
            {modelError}{' '}
            <button type="button" onClick={() => {
              void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(activeSessionId ?? '') })
              void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections })
              setModelError(null)
            }}>刷新状态</button>
          </p>
        )}
        {tab === 'map' && activeSessionId !== null ? (
          <ConversationMapPanel key={activeSessionId} sessionId={activeSessionId} scenario={scenario}
            snapshotId={snapshot?.snapshot_id ?? null} busy={running || awaitingApproval || submit.isPending}
            onContinue={(node, context) => {
              if (snapshot === null || useUiStore.getState().activeSessionId !== activeSessionId) return
              setFollowup({ sessionId: activeSessionId, turnId: node.id, question: node.question,
                ordinal: node.ordinal, context, baseSnapshotId: snapshot.snapshot_id, branchId: snapshot.branch_id ?? 'main' })
              setFocusTurn(null)
              setTab('conversation')
            }}
            onLocate={(turnId) => {
              setFollowup(null); setFocusTurn({ sessionId: activeSessionId, turnId }); setTab('conversation')
            }}
            onBranchChanged={() => { setFollowup(null); setFocusTurn(null) }} />
        ) : tab === 'trace' ? (
          <TraceView trace={trace} />
        ) : (
          <Conversation scenario={selectedFollowup !== null && snapshot?.snapshot_id === selectedFollowup.baseSnapshotId ? {
            ...scenario,
            messages: mapMessages(selectedFollowup.context).map((message) => message.kind === 'tool' ?
              { ...message, tool: { ...message.tool, expanded: expandedTools.has(message.id) } } : message),
            activity: null, empty: null, loading: null, stats: null,
            turnStats: mapTurnStats(selectedFollowup.context, mapMessages(selectedFollowup.context), false),
          } : scenario} autoFollow onToggleTool={toggleTool}
            focusTurnId={focusTurn?.sessionId === activeSessionId ? focusTurn.turnId : null} />
        )}
        {/* 地图留出详情空间；历史追问回到同一个输入框，审批时保留接管按钮。 */}
        {tab !== 'map' || awaitingApproval ? <Composer
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
            onToggleModelMenu: () => {
              setModelError(null)
              setModelMenuOpen((open) => !open)
            },
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
            // 任务进行中**允许继续写**，但不让发（T30 裁定）：草稿留在输入框里等这一轮结束。
            canSend: activeSessionId !== null && draft.trim() !== '' && !running && !awaitingApproval && !interrupted && !submit.isPending && !createSession.isPending && !setSessionMode.isPending,
          }}
        /> : null}
        </>}
        {settingsOpen ? <ModelSettingsDialog onClose={() => {
          setSettingsOpen(false)
          void queryClient.invalidateQueries({ queryKey: queryKeys.modelConnections })
          void queryClient.invalidateQueries({ queryKey: queryKeys.health })
          if (activeSessionId !== null) void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(activeSessionId) })
        }} /> : null}
      </main>
    </div>
  )
}
