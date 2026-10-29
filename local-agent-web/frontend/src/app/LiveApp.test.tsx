import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useOutbox } from '../stores/outbox'
import { useUiStore } from '../stores/ui'
import { ApiError } from '../api/client'
import { LiveApp } from './LiveApp'

const mock = vi.hoisted(() => ({
  sessionState: 'awaiting_approval' as 'awaiting_approval' | 'idle' | 'failed' | 'running',
  selection: null as null | { connection_id: string; config_version: string },
  creating: false,
  approval: null as null | Record<string, unknown>,
  messages: [] as Record<string, unknown>[],
  mutate: vi.fn(),
  mutateAsync: vi.fn(),
  acceptedJobId: null as string | null,
  taskRows: [] as { task_id: string; session_id: string; state: string }[],
  snapshotBranch: undefined as string | undefined,
  getSnapshot: vi.fn(),
  modelMutate: vi.fn(),
  modeMutate: vi.fn(),
  recoveryMutate: vi.fn(),
  // §17.4：停止这一轮。默认不 pending，好让"连点"这类断言能显式打开。
  cancelMutate: vi.fn(),
  cancelPending: false,
  cancelError: null as Error | null,
  submitReset: vi.fn(),
  approvalReset: vi.fn(),
  mode: 'agent' as 'agent' | 'chat',
  interrupted: null as string | null,
  resolve: vi.fn(),
  pickMutate: vi.fn(),
  workspaceMutate: vi.fn(),
  resolvePending: false,
  resolveError: null as null | Error,
  /**
   * 服务端说的"这一轮在不在流式"（§19.4）。`undefined` = 服务端没说（旧快照/夹具）：
   * 页面那时**不得**做任何流式声明，既有用例的默认行为因此不变。
   */
  answerStreaming: undefined as boolean | undefined,
  currentAnswerStreaming: undefined as boolean | undefined,
  /**
   * 这一轮最终结果的结局（§17.4）。`null` = 快照里没有最终结果（还没跑完）。
   * `cancelled` 用来验"被中止"与"失败"在页面上是两件事。
   */
  finalResultState: null as null | 'completed' | 'blocked' | 'failed' | 'cancelled',
  /** 临时增量尾部：只有它的存在**不足以**说明在流式（T74 用例就是冲这一点来的）。 */
  tail: null as null | {
    messageId: string
    modelCallId: string | null
    text: string
    sequence: number
    channel?: 'text' | 'reasoning'
  },
}))
vi.mock('../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../api/client')>(),
  getSessionSnapshot: mock.getSnapshot,
}))

/** 快照里的挂起审批（传输形状；页面经 `mapApproval` 映射成卡片视图模型）。 */
const pendingApproval = {
  approval_id: 'ap-1',
  run_id: 'run-1',
  tool_call_id: 't1',
  tool_name: 'delete_file',
  action_fingerprint: 'fp-1',
  redacted_arguments: { path: 'dist', recursive: 'True' },
  edited_arguments: {},
  target: 'dist',
  impact_summary: '删除 dist（递归删除）',
  risk: 'high',
  state: 'pending',
  created_at: '2026-09-27T00:00:00Z',
  note: null,
}

/** 快照里的一次已落地的编辑结果（传输形状；结构化 diff 必须来自服务端字段）。 */
const editToolMessage = {
  message_id: 'm-tool-1',
  kind: 'tool',
  created_at: '2026-09-27T00:00:00Z',
  tool: {
    tool_call_id: 't1',
    tool_name: 'edit_file',
    status: 'ok',
    redacted_arguments: { path: 'notes.txt' },
    elapsed_ms: 12,
    truncated: false,
    outcome: {
      kind: 'edit',
      target: 'notes.txt',
      summary: '替换 1 处 · 202 行变化',
      diff: {
        path: 'notes.txt',
        additions: 1,
        deletions: 201,
        hunks: [
          {
            header: '@@ -1,201 +1 @@',
            lines: [
              { kind: 'del', old_line: 1, text: 'line 1' },
              { kind: 'add', new_line: 1, text: '已批准的新内容' },
            ],
          },
        ],
      },
    },
  },
}

vi.mock('../api/queries', () => ({
  queryKeys: {
    sessions: (id: string | null) => ['sessions', id],
    workspaces: ['workspaces'],
    snapshot: (id: string) => ['snapshot', id],
    modelConnections: ['model-connections'],
    health: ['health'],
    tasks: (id: string | null) => ['tasks', id],
    changes: (id: string) => ['session-changes', id],
  },
  // 改动条与右栏预览：这个夹具里没有变更记录，两者都不渲染内容。
  useSessionChanges: () => ({ data: undefined, isLoading: false, isError: false }),
  useSessionChangeDiff: () => ({ data: undefined, isLoading: false, isError: false }),
  useHealth: () => ({ data: { model_configured: true } }),
  // 任务中心的数据源：这个夹具里没有任务，角标因此不显示（T62；§17.6）。
  useTasks: () => ({ data: { tasks: mock.taskRows, counts: {}, pending_count: 0 } }),
  useArchivedSessions: () => ({ data: [], isLoading: false, isError: false }),
  useWorkspaces: () => ({ data: [{ workspace_id: 'w1', display_name: '工作区' }] }),
  useSessions: () => ({
    data: [{
      session_id: 's1',
      title: '测试会话',
      mode: mock.mode,
      is_active: true,
      updated_at: '2026-09-27T00:00:00Z',
      workspace: { workspace_id: 'w1', display_name: '工作区' },
    }],
  }),
  useSnapshot: () => ({
    data: {
      session_id: 's1',
      title: '测试会话',
      session_state: mock.sessionState,
      interrupted_request_id: mock.interrupted,
      snapshot_id: 'snapshot-1',
      branch_id: mock.snapshotBranch,
      schema_version: 1,
      created_at: '2026-09-27T00:00:00Z',
      workspace: { workspace_id: 'w1', display_name: '工作区' },
      messages: mock.messages,
      model_selection: mock.selection,
      approval: mock.approval,
      answer_streaming: mock.answerStreaming,
      current_answer_streaming: mock.currentAnswerStreaming,
      final_result: mock.finalResultState === null ? null : {
        run_id: 'run-1', state: mock.finalResultState,
        summary: '半句回答', evidence_references: [],
      },
    },
    isFetching: false,
  }),
  useModelConnections: () => ({
    data: [
      {
        connection_id: 'mc-a', config_version: '2', display_name: '模型 A', model_id: 'model-a',
        is_default: true, status: 'verified'
      },
      {
        connection_id: 'mc-b', config_version: '3', display_name: '模型 B', model_id: 'model-b',
        is_default: false, status: 'verified'
      },
    ]
  }),
  useCreateSession: () => ({ mutate: vi.fn(), isPending: mock.creating }),
  useSetSessionMode: () => ({ mutate: mock.modeMutate, isPending: false }),
  useEndInterruptedRequest: () => ({ mutate: mock.recoveryMutate, isPending: false }),
  // §17.4 的停止：运行中的主按钮换成它。默认"没有在跑"，所以绝大多数用例照旧看到发送按钮。
  useCancelTask: () => ({ mutate: mock.cancelMutate, isPending: mock.cancelPending,
    isError: mock.cancelError !== null, error: mock.cancelError }),
  // 需要人工核对的任务：终局由人写（`POST /tasks/{id}/review`）。这个夹具里没有待核对任务，
  // 只需要存在——缺了它整个 describe 块都会因为 "No export defined" 而全红。
  useCloseTaskReview: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  // 右栏 PanelShell 会直接 import 这几个（侧聊创建、文件树、文件搜索、产物详情）：
  // 桩必须存在，否则整个用例块在模块求值阶段就因 "No export defined" 全红。
  useSideChatCreation: () => ({ create: () => Promise.resolve('side'), forget: vi.fn() }),
  useWorkspaceFiles: () => ({ data: undefined, isLoading: false, isError: false }),
  useSearchWorkspaceFiles: () => ({ data: undefined, isLoading: false, isError: false }),
  useArtifactsDetail: () => ({ data: undefined, isLoading: false, isError: false }),
  useSelectWorkspace: () => ({ mutate: mock.workspaceMutate }),
  usePickWorkspaceDirectory: () => ({ mutate: mock.pickMutate, isPending: false, error: null }),
  useSubmitMessage: (sessionId: string) => ({ acceptedJobId: mock.acceptedJobId, mutateAsync: (request: unknown) => mock.mutateAsync(sessionId, request), mutate: mock.mutate, isPending: false, isError: false, reset: mock.submitReset }),
  useSelectSessionModel: () => ({ mutate: mock.modelMutate, isPending: false }),
  useResolveApproval: () => ({
    resolve: mock.resolve,
    isPending: mock.resolvePending,
    error: mock.resolveError,
    reset: mock.approvalReset,
  }),
  // 提问答复（2026-10-28）：与审批同一套接口形状，测试默认不触发。
  useResolveQuestion: () => ({
    resolve: vi.fn(),
    isPending: false,
    error: null,
    reset: vi.fn(),
  }),
}))

vi.mock('../api/useSessionStream', () => ({
  useSessionStream: () => ({ connected: true, reconnecting: false, notices: [] }),
}))

vi.mock('../stores/stream', () => ({
  useStreamTail: () => mock.tail,
}))

beforeEach(() => {
  mock.acceptedJobId = null
  mock.taskRows = []
  mock.snapshotBranch = undefined
  mock.getSnapshot.mockReset().mockImplementation(async () => ({ snapshot_id: 'fresh-snapshot', branch_id: mock.snapshotBranch }))
  mock.sessionState = 'awaiting_approval'
  // 默认没有挂起审批：审批相关的用例各自设置，其余用例保持输入区可见。
  mock.approval = null
  mock.messages = []
  mock.resolvePending = false
  mock.resolveError = null
  mock.mutate.mockReset()
  mock.mutateAsync.mockReset().mockImplementation(() => new Promise(() => {}))
  mock.modelMutate.mockReset()
  mock.modeMutate.mockReset()
  mock.recoveryMutate.mockReset()
  mock.cancelMutate.mockReset()
  mock.cancelPending = false
  mock.cancelError = null
  mock.submitReset.mockReset()
  mock.approvalReset.mockReset()
  mock.mode = 'agent'
  mock.interrupted = null
  mock.resolve.mockReset()
  mock.pickMutate.mockReset()
  mock.workspaceMutate.mockReset()
  mock.selection = null
  mock.creating = false
  // 默认"服务端没说"与"没有尾部"：既有用例的措辞一字不变（要验 T74 的用例各自设置）。
  mock.answerStreaming = undefined
  mock.currentAnswerStreaming = undefined
  mock.finalResultState = null
  mock.tail = null
  useUiStore.setState({ workspaceId: 'w1', activeSessionId: 's1', drafts: { s1: '保留这段草稿' } })
})

describe('LiveApp pending approval', () => {
  const renderApp = () => render(
    <QueryClientProvider client={new QueryClient()}><LiveApp /></QueryClientProvider>,
  )

  // 2026-10-13：「执行方式」下拉（自由循环 / 只读调查 / 代码修改与验证 / 委派只读调查）已从输入区
  // 撤下——三类确定性工作流不再由用户手选。原先那两条"锁定控件 / 按会话隔离草稿"的用例随之删除；
  // 只读的工作流状态投影仍由 `WorkflowStatus.test.tsx` 守着。

  it('switches mode in the current session and keeps the draft', () => {
    mock.sessionState = 'idle'
    renderApp()
    // 模式切换是两段常驻控件（2026-10-13 换掉下拉）：点"闲聊"那一段就是一次切换，没有中间菜单。
    fireEvent.click(screen.getByRole('button', { name: '闲聊' }))
    expect(mock.modeMutate).toHaveBeenCalledWith({ sessionId: 's1', mode: 'chat', expectedMode: 'agent' }, expect.any(Object))
    expect(useUiStore.getState().activeSessionId).toBe('s1')
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toHaveValue('保留这段草稿')
  })

  it('blocks switching mode while a turn is running', () => {
    mock.sessionState = 'running'
    renderApp()
    // 两段都要锁：只锁一段会让人以为另一半仍可切。
    expect(screen.getByRole('button', { name: '闲聊' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Agent' })).toBeDisabled()
  })

  it('offers a stop button while the turn is running and cancels this session', () => {
    mock.sessionState = 'running'
    renderApp()

    // 运行中主按钮就是停止（§17.4）：后端有取消语义，模型调用能在流式块之间被打断。
    expect(screen.queryByRole('button', { name: '发送消息' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '停止生成' }))

    expect(mock.cancelMutate).toHaveBeenCalledWith({
      sessionId: 's1',
      reason: '用户按下了停止',
    })
  })

  it('locks the stop button while the cancel request is in flight', () => {
    mock.sessionState = 'running'
    mock.cancelPending = true
    renderApp()

    // 连点会发出第二次取消：虽然服务端是幂等的，但按钮不该鼓励这种行为。
    expect(screen.getByRole('button', { name: '停止生成' })).toBeDisabled()
  })

  it('shows a dismissible cancel failure and prevents a new send while cancellation is pending', () => {
    mock.sessionState = 'idle'
    mock.cancelPending = true
    mock.cancelError = new Error('network')
    renderApp()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('停止请求未能确认')
    fireEvent.click(screen.getByRole('button', { name: '关闭停止错误提示' }))
    expect(screen.queryByText('停止请求未能确认', { exact: false })).not.toBeInTheDocument()
  })

  it('shows a neutral 已停止 note instead of calling the cancelled turn a failure', () => {
    mock.sessionState = 'idle'
    mock.finalResultState = 'cancelled'
    renderApp()

    expect(screen.getByText('已停止')).toBeInTheDocument()
    // 它不是告警：不出现"失败"字样，也不占用错误行（§17.4 的"被中止不等于执行失败"）。
    expect(screen.queryByRole('button', { name: '停止生成' })).not.toBeInTheDocument()
  })

  it('requires explicit review before closing an interrupted request', () => {
    mock.sessionState = 'idle'
    mock.interrupted = 'request-crash'
    renderApp()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '结束中断任务' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /我已查看/ }))
    fireEvent.click(screen.getByRole('button', { name: '结束中断任务' }))
    expect(mock.recoveryMutate).toHaveBeenCalledWith({ sessionId: 's1', requestId: 'request-crash', snapshotId: 'snapshot-1' }, expect.any(Object))
    expect(mock.mutate).not.toHaveBeenCalled()
    const options = mock.recoveryMutate.mock.calls[0][1] as { onSuccess: () => void }
    act(() => options.onSuccess())
    expect(mock.submitReset).toHaveBeenCalledOnce()
    expect(mock.approvalReset).toHaveBeenCalledOnce()
  })

  it('恢复已打开会话时校准工作区，不让新会话沿用错误目标', () => {
    useUiStore.setState({ workspaceId: 'outdated-workspace', activeSessionId: 's1' })
    renderApp()
    expect(useUiStore.getState().workspaceId).toBe('w1')
  })

  it('reports a failed workspace registration after a directory was chosen', () => {
    mock.sessionState = 'idle'
    mock.pickMutate.mockImplementation((_input, options) => options.onSuccess({ path: 'C:\\missing' }))
    renderApp()

    fireEvent.click(screen.getByRole('button', { name: '添加工作区' }))
    const [, options] = mock.workspaceMutate.mock.calls[0]
    expect(options?.onError).toBeTypeOf('function')
    act(() => options.onError(
      new ApiError({ status: 404, code: 'WORKSPACE_NOT_FOUND', message: '目录不存在', hint: '重新选择目录。' }),
    ))

    expect(screen.getByText(/目录不存在.*重新选择目录/)).toBeInTheDocument()
  })

  it('shows a picker failure and clears it before the next workspace action', () => {
    mock.sessionState = 'idle'
    renderApp()

    fireEvent.click(screen.getByRole('button', { name: '添加工作区' }))
    const [, options] = mock.pickMutate.mock.calls[0]
    expect(options?.onError).toBeTypeOf('function')
    act(() => options.onError(new ApiError({
      status: 503, code: 'DIRECTORY_PICKER_UNAVAILABLE', message: '打不开目录选择器',
    })))
    expect(screen.getByText('打不开目录选择器')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '工作区' }))
    expect(screen.queryByText('打不开目录选择器')).not.toBeInTheDocument()
  })

  it('opens the session returned by the server when an existing workspace is selected', () => {
    mock.sessionState = 'idle'
    renderApp()

    fireEvent.click(screen.getByRole('button', { name: '工作区' }))
    const [, options] = mock.workspaceMutate.mock.calls[0]
    expect(options?.onSuccess).toBeTypeOf('function')
    act(() => options.onSuccess({
      session_id: 's2',
      workspace: { workspace_id: 'w1', display_name: '工作区' },
    }))

    expect(useUiStore.getState().activeSessionId).toBe('s2')
  })

  it('lets the real approval take over the composer and keeps the draft untouched', () => {
    mock.approval = { ...pendingApproval }
    renderApp()

    // 侧栏与卡片都会说"等待审批"，所以这里按卡片自己的内容断言。
    expect(screen.getByText('删除 dist（递归删除）')).toBeInTheDocument()
    expect(screen.getByText('删除文件')).toBeInTheDocument()
    expect(screen.getByText('dist')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '进入二次确认' })).toBeInTheDocument()
    // 有待决审批时输入区被审批卡接管；草稿留在 store 里，没有被吞掉。
    expect(screen.queryByRole('textbox', { name: '给智能体发消息' })).not.toBeInTheDocument()
    expect(useUiStore.getState().drafts.s1).toBe('保留这段草稿')
    expect(mock.mutate).not.toHaveBeenCalled()
  })

  it('submits the decision the user took, with that approval id', () => {
    mock.approval = { ...pendingApproval }
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: '进入二次确认' }))
    expect(mock.resolve).toHaveBeenCalledWith('ap-1', 'approve', false)

    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    expect(mock.resolve).toHaveBeenCalledWith('ap-1', 'reject', false)
  })

  it('sends recursive confirmation only after the second-stage checkbox', () => {
    mock.approval = { ...pendingApproval, confirmation_stage: 2 }
    renderApp()
    const confirm = screen.getByRole('button', { name: '确认删除' })
    expect(confirm).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: '我确认删除该目录及其全部内容' }))
    fireEvent.click(confirm)
    expect(mock.resolve).toHaveBeenCalledWith('ap-1', 'approve', true)
  })

  it('locks both buttons while the decision is in flight', () => {
    mock.approval = { ...pendingApproval }
    mock.resolvePending = true
    renderApp()
    expect(screen.getByRole('button', { name: '进入二次确认' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '进入二次确认' }))
    expect(mock.resolve).not.toHaveBeenCalled()
  })

  it('restores the composer once the request is no longer actionable', () => {
    mock.approval = {
      ...pendingApproval,
      state: 'expired',
      note: '该审批已失效，未执行任何操作。请重新发起请求。',
    }
    renderApp()

    // 失效后输入区回来（卡片留在其上方说明结果），草稿仍在。
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toHaveValue('保留这段草稿')
    expect(screen.getByText('审批已失效')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
  })

  it('explains a stale decision instead of silently retrying（文案取自后端）', () => {
    mock.approval = { ...pendingApproval }
    // 2026-10-28：页面不再按错误码自己写文案，直接展示后端给的 message + hint。
    mock.resolveError = new ApiError({
      status: 409,
      code: 'APPROVAL_EXPIRED',
      message: '这个审批请求已经不在等待决策，因此没有执行任何操作。',
      hint: '重新发起请求并重新确认。',
    })
    renderApp()
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('这个审批请求已经不在等待决策')
    expect(alert).toHaveTextContent('重新发起请求并重新确认')
  })

  it('reveals the structured diff only after the tool row is expanded', () => {
    mock.sessionState = 'idle'
    mock.messages = [editToolMessage]
    renderApp()

    // 收起时只有一行摘要：结构化 diff 不在 DOM 里，也就没有机会被读成"已经确认过"。
    expect(screen.queryByText('已批准的新内容')).not.toBeInTheDocument()
    const row = screen.getByRole('button', { name: /修改文件/ })
    expect(row).toHaveAttribute('aria-expanded', 'false')
    // 但**行上**要直接给出这次改了多少行（2026-10-21）：点开之前也该看得到 +/-。
    expect(screen.getByText('−201')).toBeInTheDocument()

    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('已批准的新内容')).toBeInTheDocument()
    // 行号与增减计数都来自服务端字段，页面不自己算；展开后行上与卡片里各一份。
    expect(screen.getAllByText('−201')).toHaveLength(2)

    fireEvent.click(row)
    expect(screen.queryByText('已批准的新内容')).not.toBeInTheDocument()
  })

  it('restores a submitted draft after a rejected request without overwriting newer typing', () => {
    mock.sessionState = 'idle'
    mock.mutate.mockImplementation((_text: string, options: { onError: () => void }) => {
      options.onError()
    })
    renderApp()

    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(useUiStore.getState().drafts.s1).toBe('保留这段草稿')

    mock.mutate.mockImplementation((_text: string, options: { onError: () => void }) => {
      useUiStore.getState().setDraft('s1', '执行期间的新草稿')
      options.onError()
    })
    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(useUiStore.getState().drafts.s1).toBe('执行期间的新草稿')
  })

  it('sends the quoted answer as a Markdown quote and clears the quote row', () => {
    mock.sessionState = 'idle'
    mock.messages = [
      { message_id: 'u1', kind: 'user', created_at: '2026-10-26T00:00:00Z', text: '这段怎么理解' },
      { message_id: 'a1', kind: 'assistant', created_at: '2026-10-26T00:00:01Z',
        text: '第一段回答\n\n第二段回答', complete: true },
    ]
    renderApp()

    fireEvent.click(screen.getByRole('button', { name: '追问' }))
    expect(screen.getByRole('note', { name: '引用内容' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    const payload = mock.mutate.mock.calls[0]?.[0] as { text: string }
    // 引用是 Markdown 引用块（空行写成 `>`，否则引用块会被空行截断），问题跟在后面空一行。
    expect(payload.text).toBe('> 第一段回答\n>\n> 第二段回答\n\n保留这段草稿')
    // 发出后引用条消失：它是这条消息的一部分，不是常驻状态。
    expect(screen.queryByRole('note', { name: '引用内容' })).not.toBeInTheDocument()
  })

  it('selects a session model with both observed and target versions', () => {
    mock.sessionState = 'idle'
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: '选择会话模型' }))
    expect(screen.getByRole('button', { name: '选择会话模型' })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByRole('button', { name: /模型 Bmodel-b/ }))
    expect(mock.modelMutate).toHaveBeenCalledWith({
      connectionId: 'mc-b',
      expectation: { connection_id: null, config_version: null },
      expectedVersion: 3,
    }, expect.any(Object))
  })

  it.each(['button', 'escape'])('closes the model menu with %s without changing the draft or model', (method) => {
    mock.sessionState = 'idle'
    renderApp()
    const trigger = screen.getByRole('button', { name: '选择会话模型' })
    fireEvent.click(trigger)
    const close = screen.getByRole('button', { name: '关闭模型选择' })
    close.focus()
    if (method === 'button') fireEvent.click(close)
    else fireEvent.keyDown(close, { key: 'Escape' })
    expect(screen.queryByRole('region', { name: '模型选择' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toHaveValue('保留这段草稿')
    expect(mock.modelMutate).not.toHaveBeenCalled()
    expect(mock.mutate).not.toHaveBeenCalled()
  })

  it('restores follow-default with the observed explicit selection', () => {
    mock.sessionState = 'idle'
    mock.selection = { connection_id: 'mc-b', config_version: '3' }
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: '选择会话模型' }))
    fireEvent.click(screen.getByRole('button', { name: '恢复跟随默认模型' }))
    expect(mock.modelMutate).toHaveBeenCalledWith({
      connectionId: null,
      expectation: { connection_id: 'mc-b', config_version: 3 },
      expectedVersion: null,
    }, expect.any(Object))
  })

  it('explains a failed empty answer even when the snapshot has no error row', () => {
    mock.sessionState = 'failed'
    renderApp()
    // 2026-09-30：旧文案断言"请检查模型连接状态"，而这一支最常见的成因是**本轮达到调用上限**
    // 或模型调用失败，把用户引向"模型连接"是错误的归因（实测：模型完全正常、也已给出收尾回答）。
    // 这里只钉住"页面必须解释这一轮为什么没给出回答"，措辞可再改。
    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent('这轮在给出最终回答前就结束了')
    expect(notice).not.toHaveTextContent('请检查模型连接状态')
  })

  it('keeps a running turn pinned without pretending to know its model name', () => {
    mock.sessionState = 'running'
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: '选择会话模型' }))
    expect(screen.getByText(/正在运行的一轮：连接版本已固定/)).toBeInTheDocument()
    expect(screen.queryByText(/正在运行的一轮：模型 B/)).not.toBeInTheDocument()
  })

  it('shows a selection conflict and asks to refresh instead of silently retrying', () => {
    mock.sessionState = 'idle'
    mock.modelMutate.mockImplementation((_choice: unknown, options: { onError: (error: Error) => void }) => {
      options.onError(new ApiError({ status: 409, code: 'CONFIG_CONFLICT', message: '已冲突' }))
    })
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: '选择会话模型' }))
    fireEvent.click(screen.getByRole('button', { name: /模型 Bmodel-b/ }))
    expect(screen.getByRole('alert')).toHaveTextContent('模型选择已被其他页面修改')
    expect(screen.getByRole('button', { name: '刷新状态' })).toBeInTheDocument()
    expect(mock.modelMutate).toHaveBeenCalledTimes(1)
  })
})

/**
 * 未流式（v0.8 T74；契约 §19.4）。
 *
 * 这一组守的是"不假装在流"：当前轮判据来自服务端的 `current_answer_streaming`，
 * 而"有增量"或"消息没完"都不算判据——后者在刚连上、网络慢、回答极短时都会给出错误答案。
 */
describe('LiveApp 未流式', () => {
  const renderApp = () => render(
    <QueryClientProvider client={new QueryClient()}><LiveApp /></QueryClientProvider>,
  )

  it('says the answer arrives in one piece instead of leaving the user guessing', () => {
    mock.sessionState = 'running'
    mock.answerStreaming = false
    mock.currentAnswerStreaming = false
    renderApp()

    expect(screen.getByText('未启用流式：回答会在完成时一次性显示。')).toBeInTheDocument()
    expect(screen.queryByText('正在接收回答')).not.toBeInTheDocument()
  })

  it('never claims to be receiving increments while the server says it is not streaming', () => {
    // 关键反例：**即使**此刻手上有尾部文本，服务端说没在流式时也不能说"正在接收回答"。
    mock.sessionState = 'running'
    mock.answerStreaming = false
    mock.currentAnswerStreaming = false
    mock.tail = { messageId: 'a-1', modelCallId: 'call-1', text: '增量文本', sequence: 1 }
    renderApp()

    expect(screen.queryByText('正在接收回答')).not.toBeInTheDocument()
    expect(screen.queryByText('正在思考')).not.toBeInTheDocument()
    expect(screen.getByText('正在处理请求')).toBeInTheDocument()
  })

  it('keeps the increment wording when the server says it is streaming', () => {
    // 另一半：守卫不是把这句话一律删掉——服务端说在流式且确实有增量时，照旧说"正在接收回答"。
    mock.sessionState = 'running'
    mock.answerStreaming = true
    mock.currentAnswerStreaming = true
    mock.tail = { messageId: 'a-1', modelCallId: 'call-1', text: '增量文本', sequence: 1 }
    renderApp()

    expect(screen.getByText('正在接收回答')).toBeInTheDocument()
    expect(screen.queryByText('未启用流式：回答会在完成时一次性显示。')).not.toBeInTheDocument()
  })

  it('keeps the current stream wording after switching the next model to non-streaming', () => {
    mock.sessionState = 'running'
    mock.answerStreaming = false
    mock.currentAnswerStreaming = true
    mock.tail = { messageId: 'a-1', modelCallId: 'call-1', text: '当前轮增量', sequence: 1 }
    renderApp()

    expect(screen.getByText('正在接收回答')).toBeInTheDocument()
    expect(screen.queryByText('未启用流式：回答会在完成时一次性显示。')).not.toBeInTheDocument()
    expect(screen.getByText('未流式')).toBeInTheDocument() // 下一轮模型的输入区标注。
  })

  it('keeps the current non-streaming notice after switching the next model to streaming', () => {
    mock.sessionState = 'running'
    mock.answerStreaming = true
    mock.currentAnswerStreaming = false
    renderApp()

    expect(screen.getByText('未启用流式：回答会在完成时一次性显示。')).toBeInTheDocument()
    expect(screen.queryByText('未流式')).not.toBeInTheDocument()
  })

  it('marks the session as 未流式 next to the model, and only when the server said so', () => {
    mock.answerStreaming = false
    const notStreaming = renderApp()
    expect(screen.getByText('未流式')).toBeInTheDocument()
    notStreaming.unmount()

    // 服务端说在流式 / 没说（旧快照）：不显示这个标注，也不给相反暗示。
    mock.answerStreaming = true
    const streaming = renderApp()
    expect(screen.queryByText('未流式')).not.toBeInTheDocument()
    streaming.unmount()

    mock.answerStreaming = undefined
    renderApp()
    expect(screen.queryByText('未流式')).not.toBeInTheDocument()
  })
})

describe('LiveApp 待发队列（2026-10-28）', () => {
  it('版本冲突后可以重试，但不能悄悄把消息发到另一个分支', async () => {
    mock.sessionState = 'idle'
    mock.snapshotBranch = 'original'
    mock.mutateAsync.mockImplementationOnce(() => {
      mock.snapshotBranch = 'changed'
      return Promise.reject(new ApiError({ status: 409, code: 'CONVERSATION_CONFLICT', message: '状态已更新' }))
    })
    useOutbox.getState().enqueue('s1', '属于原分支的消息')
    render(<QueryClientProvider client={new QueryClient()}><LiveApp /></QueryClientProvider>)
    await waitFor(() => expect(useOutbox.getState().paused.s1).toBe(true), { timeout: 3000 })
    expect(mock.mutateAsync).toHaveBeenCalledOnce()
    expect(screen.getByText(/会话分支已切换/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '继续发送' }))
    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledTimes(2))
    expect(mock.mutateAsync.mock.calls[1]?.[1].context.expected_branch_id).toBe('changed')
  })
  it('后台已接受时不使用旧空闲快照，直到本次任务确认结束才发送队首', async () => {
    mock.sessionState = 'idle'
    mock.acceptedJobId = 'job-accepted'
    useOutbox.getState().enqueue('s1', '必须等本轮结束')
    render(<QueryClientProvider client={new QueryClient()}><LiveApp /></QueryClientProvider>)
    expect(mock.mutateAsync).not.toHaveBeenCalled()
    act(() => {
      mock.taskRows = [{ task_id: 'job-accepted', session_id: 's1', state: 'running' }]
      useUiStore.setState({ drafts: { s1: '触发刷新' } })
    })
    expect(mock.mutateAsync).not.toHaveBeenCalled()
    act(() => {
      mock.taskRows = [{ task_id: 'job-accepted', session_id: 's1', state: 'completed' }]
      useUiStore.setState({ drafts: { s1: '' } })
    })
    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledOnce())
  })
  const renderApp = () => render(
    <QueryClientProvider client={new QueryClient()}><LiveApp /></QueryClientProvider>,
  )

  beforeEach(() => {
    useOutbox.getState().reset()
  })

  it('切会话并再次提交后，原会话的失败仍恢复队首，且不阻塞另一会话', async () => {
    mock.sessionState = 'idle'
    let rejectFirst!: (error: Error) => void
    mock.mutateAsync.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
    useOutbox.getState().enqueue('s1', 'A 的队首')
    renderApp()
    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledTimes(1))
    act(() => {
      useUiStore.getState().openSession('s2')
      useOutbox.getState().enqueue('s2', 'B 的队首')
    })
    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledTimes(2))
    expect(mock.mutateAsync.mock.calls[1]?.[0]).toBe('s2')
    act(() => rejectFirst(new Error('A 断网')))
    await waitFor(() => expect(useOutbox.getState().paused.s1).toBe(true))
    expect(useOutbox.getState().bySession.s1?.[0]?.text).toBe('A 的队首')
    expect(screen.queryByText(/A 断网/)).not.toBeInTheDocument()
    act(() => useOutbox.getState().enqueue('s2', 'B 的下一条'))
    expect(mock.mutateAsync).toHaveBeenCalledTimes(2)
    act(() => {
      useUiStore.getState().openSession('s1')
      useOutbox.getState().resume('s1')
    })
    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledTimes(3))
    expect(mock.mutateAsync.mock.calls[2]?.[0]).toBe('s1')
  })

  it('发送队首那条，并在本轮结束后自动发出去', async () => {
    mock.sessionState = 'idle'
    useOutbox.getState().enqueue('s1', '下一条')
    useOutbox.getState().enqueue('s1', '再下一条')

    renderApp()

    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledTimes(1))
    expect(mock.mutateAsync.mock.calls[0]?.[1]).toEqual({ text: '下一条', attachments: [],
      context: { expected_snapshot_id: 'fresh-snapshot', expected_branch_id: undefined } })
    // 只有发出去的那条离开队列，后面的还在等下一轮。
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['再下一条'])
  })

  it('本轮进行中不动队列，等它结束', async () => {
    mock.sessionState = 'running'
    useOutbox.getState().enqueue('s1', '等一会儿')

    renderApp()

    await waitFor(() => expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeInTheDocument())
    expect(mock.mutateAsync).not.toHaveBeenCalled()
    expect(useOutbox.getState().bySession.s1).toHaveLength(1)
  })

  it('队列满时明确说"没有加进去"，不静默丢输入', async () => {
    mock.sessionState = 'running'
    for (let index = 0; index < 20; index += 1) {
      useOutbox.getState().enqueue('s1', `第 ${index} 条`)
    }
    useUiStore.setState({ drafts: { s1: '再加一条' } })

    renderApp()
    fireEvent.keyDown(screen.getByRole('textbox', { name: '给智能体发消息' }), { key: 'Enter' })

    expect(await screen.findByText(/待发队列已满/)).toBeInTheDocument()
    expect(useOutbox.getState().bySession.s1).toHaveLength(20)
  })

  it('自动发送失败时说明原因（只给中文，不嵌服务端原文），并把消息放回队首', async () => {
    mock.sessionState = 'idle'
    mock.mutateAsync.mockImplementation(() => {
      // 后端给的是面向用户的一句话（§15.5）；页面原样展示，不再自己写码表。
      return Promise.reject(new ApiError({
        status: 409,
        code: 'MODEL_AUTH_FAILED',
        message: '测试：模型鉴权失败，同一会话同一时刻只允许一轮运行',
        hint: '等待当前一轮结束。',
      }))
    })
    useOutbox.getState().enqueue('s1', '下一条')

    renderApp()

    const notice = await screen.findByText(/队列里的下一条没有发出去/)
    expect(notice).toHaveTextContent('同一会话同一时刻只允许一轮运行')
    // 队列提示只取 message：可执行提示由"已暂停，处理后再点继续发送"承担，不重复后端 hint。
    expect(notice).not.toHaveTextContent('等待当前一轮结束')
    expect(notice).not.toHaveTextContent('sk-')
    expect(useOutbox.getState().paused.s1).toBe(true)
    // 消息没有丢：还在队首。
    expect(useOutbox.getState().bySession.s1?.map((item) => item.text)).toEqual(['下一条'])
  })

  it('用户按过停止之后保持暂停，直到点"继续发送"', async () => {
    mock.sessionState = 'idle'
    useOutbox.getState().enqueue('s1', '暂停中')
    useOutbox.getState().pause('s1')

    renderApp()

    await waitFor(() => expect(screen.getByRole('button', { name: '继续发送' })).toBeInTheDocument())
    expect(mock.mutateAsync).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: '继续发送' }))
    await waitFor(() => expect(mock.mutateAsync).toHaveBeenCalledTimes(1))
  })
})
