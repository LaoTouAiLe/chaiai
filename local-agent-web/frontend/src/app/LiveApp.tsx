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
import {
  mapApproval,
  mapContext,
  mapError,
  mapMessages,
  mapPlan,
  mapSession,
  mapStats,
  mapTurnStats,
  modelLabelFor,
} from '../features/conversation/adapters'
import { mapSidebar } from '../features/sidebar/adapters'
import { ModelSettingsDialog } from '../features/settings/ModelSettingsDialog'
import { parseSelectionVersion, selectionExpectation, toModelMenu } from '../features/settings/modelMenuAdapter'
import { ApiError } from '../api/client'
import { queryKeys, useHealth, useModelConnections, useSessions, useSnapshot, useSubmitMessage, useWorkspaces, useCreateSession, useSelectWorkspace, useSelectSessionModel, useResolveApproval } from '../api/queries'
import { useSessionStream } from '../api/useSessionStream'
import { useStreamTail } from '../stores/stream'
import { useUiStore } from '../stores/ui'
import type { ConversationScenario, MessageView } from '../fixtures/types'
import styles from './App.module.css'

export function LiveApp() {
  const workspaceId = useUiStore((state) => state.workspaceId)
  const activeSessionId = useUiStore((state) => state.activeSessionId)
  const drafts = useUiStore((state) => state.drafts)
  const setWorkspace = useUiStore((state) => state.setWorkspace)
  const openSession = useUiStore((state) => state.openSession)
  const setDraft = useUiStore((state) => state.setDraft)

  const [settingsOpen, setSettingsOpen] = useState(false)
  const [modelMenuOpen, setModelMenuOpen] = useState(false)
  const [modelError, setModelError] = useState<string | null>(null)
  // 工具行的展开态：**纯界面状态**，不来自快照。T32 的结构化 diff 就在展开后才可见
  // （`ToolRow` 收起时只有一行摘要），所以真会话必须能让用户按需展开。
  const [expandedTools, setExpandedTools] = useState<ReadonlySet<string>>(() => new Set())
  const queryClient = useQueryClient()

  const health = useHealth()
  const workspaces = useWorkspaces()
  const sessions = useSessions(workspaceId)
  const snapshotQuery = useSnapshot(activeSessionId)
  const connections = useModelConnections()
  const createSession = useCreateSession()
  const selectWorkspace = useSelectWorkspace()
  const submit = useSubmitMessage(activeSessionId)
  const selectSessionModel = useSelectSessionModel(activeSessionId)
  const approvalDecision = useResolveApproval(activeSessionId)

  const stream = useSessionStream(activeSessionId)
  const tail = useStreamTail(activeSessionId)

  // 稳定引用：`?? []` 每次渲染都会产生新数组，会让下面的 effect/memo 每次都重跑。
  const sessionList = useMemo(() => sessions.data ?? [], [sessions.data])
  const snapshot = snapshotQuery.data ?? null

  // 刷新恢复：本地没有选中项时，按服务端的 `is_active` 回填（全库唯一 active）。
  useEffect(() => {
    if (activeSessionId !== null) return
    const active = sessionList.find((item) => item.is_active === true)
    if (active === undefined) return
    openSession(active.session_id)
    setWorkspace(active.workspace?.workspace_id ?? null)
  }, [activeSessionId, openSession, sessionList, setWorkspace])

  // 没有显式工作区时跟随第一个：服务端可能已经有启动默认工作区。
  useEffect(() => {
    if (workspaceId !== null) return
    const first = (workspaces.data ?? [])[0]
    if (first === undefined) return
    setWorkspace(first.workspace_id)
  }, [setWorkspace, workspaceId, workspaces.data])

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

    const messages = snapshot === null ? [] : [...mapMessages(snapshot, tail), ...errors]
      // 展开态在这里注入：适配层只给"默认紧凑"，被点过的行按界面状态覆盖。
      .map((message) =>
        message.kind === 'tool'
          ? { ...message, tool: { ...message.tool, expanded: expandedTools.has(message.id) } }
          : message,
      )

    // 安全阶段摘要（§6.7）：只表达"现在在做什么"，不暴露节点名或隐藏推理。
    const toolInFlight = messages.some((message) => message.kind === 'tool' && message.tool.status === 'running')

    return {
      id: 'live',
      purpose: '',
      sessionId: activeSessionId,
      session: session ?? { title: '新会话', workspace: null, status: 'idle', modelLabel: '' },
      messages,
      plan: snapshot === null ? null : mapPlan(snapshot.plan),
      stats: snapshot === null || (snapshot.turn_usages ?? []).some((turn) => turn.usage.run_id === snapshot.usage?.run_id)
        ? null : mapStats(snapshot),
      turnStats: snapshot === null ? [] : mapTurnStats(snapshot, messages),
      context: snapshot === null ? { usedTokens: null, windowTokens: null, scope: 'unknown' } : mapContext(snapshot),
      takeover: approvalView === null ? { kind: 'none' } : { kind: 'approval', approval: approvalView },
      settings: null,
      modelMenu,
      composer: {
        disabledReason:
          createSession.isPending
            ? '正在创建会话，请稍候。'
            : health.data?.model_configured === false
              ? '还没有可用的模型连接：点左下角「设置」加一条，保存后就能直接对话。'
              : null,
        phase: running ? (toolInFlight ? '正在调用工具…' : '正在生成回答…') : null,
      },
      empty: messages.length === 0 ? empty : null,
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
    createSession.isPending,
    expandedTools,
    health.data?.model_configured,
    modelMenu,
    running,
    session,
    snapshot,
    snapshotQuery.isFetching,
    tail,
  ])

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
    if (activeSessionId === null || draft.trim() === '' || running || awaitingApproval || submit.isPending || createSession.isPending) return
    const text = draft
    // 先清草稿：提交的幂等键重试由用户发起（见 queries.ts），草稿不该在失败后被吞掉，
    // 但也不该在成功后又发一遍——所以这里清掉，失败时用户可以重新输入。
    setDraft(activeSessionId, '')
    submit.mutate(text, {
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

  return (
    <div className={styles.app} data-scenario="live">
      <Sidebar
        workspaces={sidebar}
        actions={{
          onSelectWorkspace: (id) => {
            setWorkspace(id)
            selectWorkspace.mutate({ workspace_id: id })
          },
          onSelectSession: (id) => openSession(id),
          onNewSession: () => {
            if (createSession.isPending) return
            createSession.mutate(workspaceId, {
              onSuccess: (created) => openSession(created.session_id),
            })
          },
          onOpenSettings: () => setSettingsOpen(true),
        }}
      />
      <main className={styles.main}>
        <TopBar
          title={session?.title ?? '新会话'}
          workspace={session?.workspace ?? null}
          activeTab="conversation"
        />
        {stream.notices.length === 0 ? null : (
          // 传输层提示（超限/空闲超时/无法解析的帧）如实显示：它们不代表数据损坏，但用户有权知道。
          <p className={styles.notice} role="status">
            事件流提示：{stream.notices.join('、')}
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
        {failedWithoutDetail ? (
          <p className={styles.notice} role="alert">
            这轮没有生成可展示的最终回答。请检查模型连接状态后再发起新请求。
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
        <Conversation scenario={scenario} autoFollow onToggleTool={toggleTool} />
        <Composer
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
            // 任务进行中**允许继续写**，但不让发（T30 裁定）：草稿留在输入框里等这一轮结束。
            canSend: activeSessionId !== null && draft.trim() !== '' && !running && !awaitingApproval && !submit.isPending && !createSession.isPending,
          }}
        />
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
