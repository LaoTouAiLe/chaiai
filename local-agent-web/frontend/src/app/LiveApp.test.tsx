import { act, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useUiStore } from '../stores/ui'
import { ApiError } from '../api/client'
import { LiveApp } from './LiveApp'

const mock = vi.hoisted(() => ({
  sessionState: 'awaiting_approval' as 'awaiting_approval' | 'idle' | 'failed' | 'running',
  selection: null as null | { connection_id: string; config_version: string },
  approval: null as null | Record<string, unknown>,
  messages: [] as Record<string, unknown>[],
  mutate: vi.fn(),
  modelMutate: vi.fn(),
  modeMutate: vi.fn(),
  recoveryMutate: vi.fn(),
  submitReset: vi.fn(),
  approvalReset: vi.fn(),
  mode: 'agent' as 'agent' | 'chat',
  interrupted: null as string | null,
  resolve: vi.fn(),
  pickMutate: vi.fn(),
  workspaceMutate: vi.fn(),
  resolvePending: false,
  resolveError: null as null | Error,
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
  },
  useHealth: () => ({ data: { model_configured: true } }),
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
      schema_version: 1,
      created_at: '2026-09-27T00:00:00Z',
      workspace: { workspace_id: 'w1', display_name: '工作区' },
      messages: mock.messages,
      model_selection: mock.selection,
      approval: mock.approval,
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
  useCreateSession: () => ({ mutate: vi.fn() }),
  useSetSessionMode: () => ({ mutate: mock.modeMutate, isPending: false }),
  useEndInterruptedRequest: () => ({ mutate: mock.recoveryMutate, isPending: false }),
  useSelectWorkspace: () => ({ mutate: mock.workspaceMutate }),
  usePickWorkspaceDirectory: () => ({ mutate: mock.pickMutate, isPending: false, error: null }),
  useSubmitMessage: () => ({ mutate: mock.mutate, isPending: false, isError: false, reset: mock.submitReset }),
  useSelectSessionModel: () => ({ mutate: mock.modelMutate, isPending: false }),
  useResolveApproval: () => ({
    resolve: mock.resolve,
    isPending: mock.resolvePending,
    error: mock.resolveError,
    reset: mock.approvalReset,
  }),
}))

vi.mock('../api/useSessionStream', () => ({
  useSessionStream: () => ({ connected: true, notices: [] }),
}))

vi.mock('../stores/stream', () => ({
  useStreamTail: () => null,
}))

beforeEach(() => {
  mock.sessionState = 'awaiting_approval'
  // 默认没有挂起审批：审批相关的用例各自设置，其余用例保持输入区可见。
  mock.approval = null
  mock.messages = []
  mock.resolvePending = false
  mock.resolveError = null
  mock.mutate.mockReset()
  mock.modelMutate.mockReset()
  mock.modeMutate.mockReset()
  mock.recoveryMutate.mockReset()
  mock.submitReset.mockReset()
  mock.approvalReset.mockReset()
  mock.mode = 'agent'
  mock.interrupted = null
  mock.resolve.mockReset()
  mock.pickMutate.mockReset()
  mock.workspaceMutate.mockReset()
  mock.selection = null
  useUiStore.setState({ workspaceId: 'w1', activeSessionId: 's1', drafts: { s1: '保留这段草稿' } })
})

describe('LiveApp pending approval', () => {
  const renderApp = () => render(
    <QueryClientProvider client={new QueryClient()}><LiveApp /></QueryClientProvider>,
  )

  it('switches mode in the current session and keeps the draft', () => {
    mock.sessionState = 'idle'
    renderApp()
    fireEvent.click(screen.getByRole('button', { name: '选择会话模式' }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /闲聊/ }))
    expect(mock.modeMutate).toHaveBeenCalledWith({ sessionId: 's1', mode: 'chat', expectedMode: 'agent' }, expect.any(Object))
    expect(useUiStore.getState().activeSessionId).toBe('s1')
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toHaveValue('保留这段草稿')
  })

  it('blocks switching mode while a turn is running', () => {
    mock.sessionState = 'running'
    renderApp()
    expect(screen.getByRole('button', { name: '选择会话模式' })).toBeDisabled()
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

  it('explains a stale decision instead of silently retrying', () => {
    mock.approval = { ...pendingApproval }
    mock.resolveError = new ApiError({ status: 409, code: 'APPROVAL_EXPIRED', message: '已失效' })
    renderApp()
    expect(screen.getByRole('alert')).toHaveTextContent('这次审批已失效，未执行任何操作')
  })

  it('reveals the structured diff only after the tool row is expanded', () => {
    mock.sessionState = 'idle'
    mock.messages = [editToolMessage]
    renderApp()

    // 收起时只有一行摘要：结构化 diff 不在 DOM 里，也就没有机会被读成"已经确认过"。
    expect(screen.queryByText('已批准的新内容')).not.toBeInTheDocument()
    const row = screen.getByRole('button', { name: /修改文件/ })
    expect(row).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(row)
    expect(row).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('已批准的新内容')).toBeInTheDocument()
    // 行号与增减计数都来自服务端字段，页面不自己算。
    expect(screen.getByText('−201')).toBeInTheDocument()

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
