import { CancelledError, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionStream } from './useSessionStream'
import { queryKeys } from './queries'
import { useStreamStore } from '../stores/stream'

const mock = vi.hoisted(() => ({
  getSnapshot: vi.fn(),
  read: vi.fn(),
}))

vi.mock('./client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./client')>()),
  getSessionSnapshot: mock.getSnapshot,
}))

vi.mock('./events', () => ({ readSessionEvents: mock.read }))

beforeEach(() => {
  mock.getSnapshot.mockReset()
  mock.getSnapshot.mockResolvedValue({ session_id: 's1', session_state: 'awaiting_approval', messages: [] })
  mock.read.mockReset()
  useStreamStore.setState({ tails: {} })
})

describe('useSessionStream approval boundary', () => {
  it.each(['approval_required', 'approval_resolved'] as const)(
    'refreshes the authoritative snapshot on %s',
    async (eventType) => {
      mock.read.mockImplementation(async (_sessionId, handlers: { onEvent: (event: unknown) => void }) => {
        handlers.onEvent({ event_id: 'e-1', event_type: eventType, payload: {} })
      })
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const wrapper = ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      )

      renderHook(() => useSessionStream('s1'), { wrapper })

      await waitFor(() => expect(mock.getSnapshot).toHaveBeenCalledTimes(2))
      expect(mock.read).toHaveBeenCalledTimes(1)
    },
  )

  it('applies a named answer delta to the temporary tail', async () => {
    mock.read.mockImplementation(async (_sessionId, handlers: { onEvent: (event: unknown) => void }) => {
      handlers.onEvent({
        event_id: 'e-1', event_type: 'message_started', payload: {
          role: 'assistant', message_id: 'a-1', model_call_id: 'call-1',
        }
      })
      handlers.onEvent({
        event_id: 'e-2', event_type: 'message_delta', payload: {
          role: 'assistant', message_id: 'a-1', model_call_id: 'call-1', chunk_sequence: 1, text_delta: '正在生成',
        }
      })
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    const { unmount } = renderHook(() => useSessionStream('s1'), { wrapper })
    await waitFor(() => expect(useStreamStore.getState().tails.s1?.text).toBe('正在生成'))
    unmount()
  })

  it('reports a failed reconcile, but not a cancelled one', async () => {
    // 切会话/卸载会让这个查询的最后一个观察者消失，React Query 据此取消在飞的对账请求。
    // 取消是既定行为，不是故障：提示出来用户会在新会话上看到一条英文错误码，而什么都没坏。
    mock.read.mockImplementation(async (_sessionId, handlers: { onEvent: (event: unknown) => void }) => {
      handlers.onEvent({ event_id: 'e-1', event_type: 'task_failed', payload: {} })
    })
    mock.getSnapshot
      .mockResolvedValueOnce({ session_id: 's1', session_state: 'failed', messages: [] })
      .mockRejectedValue(new CancelledError())
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    const { result, unmount } = renderHook(() => useSessionStream('s1'), { wrapper })

    await waitFor(() => expect(mock.getSnapshot).toHaveBeenCalledTimes(2))
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(result.current.notices).toEqual([])
    unmount()
  })

  it.each(['job_queued', 'job_progress', 'job_waiting', 'job_completed', 'task_cancelled'] as const)(
    'invalidates the task centre on %s instead of deriving task state locally',
    async (eventType) => {
      mock.read.mockImplementation(async (_sessionId, handlers: { onEvent: (event: unknown) => void }) => {
        handlers.onEvent({ event_id: 'e-1', event_type: eventType, payload: {} })
      })
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const invalidate = vi.spyOn(client, 'invalidateQueries')
      const wrapper = ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      )

      const { unmount } = renderHook(() => useSessionStream('s1'), { wrapper })

      // 任务状态的唯一出口是索引：页面只失效查询、自己**不推导**状态
      await waitFor(() => {
        expect(invalidate).toHaveBeenCalledWith({ queryKey: ['tasks'] })
      })
      unmount()
    },
  )

  it('reconnects after a normal stream close, fetching a snapshot before each subscription', async () => {
    mock.read.mockResolvedValue(undefined)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    const { unmount } = renderHook(() => useSessionStream('s1'), { wrapper })
    await waitFor(() => expect(mock.read).toHaveBeenCalledTimes(2), { timeout: 2000 })
    expect(mock.getSnapshot).toHaveBeenCalledTimes(2)
    unmount()
  })

  it('同一个事件重复投递只应用一次（重连后的历史重放是常态，不是新事实）', async () => {
    mock.read.mockImplementation(async (_sessionId, handlers: { onEvent: (event: unknown) => void }) => {
      const event = { event_id: 'e-same', event_type: 'approval_required', payload: {} }
      handlers.onEvent(event)
      handlers.onEvent(event) // 再投影一次：不得因此再取一次快照
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    renderHook(() => useSessionStream('s1'), { wrapper })

    // 连接那一次 + 事件触发的**一次**对账；重复投递不产生第二个效果（§19.1）
    await waitFor(() => expect(mock.getSnapshot).toHaveBeenCalledTimes(2))
    expect(mock.read).toHaveBeenCalledTimes(1)
  })

  it('重连时把本地尾部对账到权威快照（历史重放不把文本再拼一遍）', async () => {
    mock.read.mockResolvedValue(undefined) // 正常收尾 ⇒ 500ms 后重订阅
    useStreamStore.setState({
      tails: { s1: { messageId: 'stale', modelCallId: null, text: '上一轮残留', sequence: 3 } },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    const { unmount } = renderHook(() => useSessionStream('s1'), { wrapper })

    // 快照里没有这条未完成消息 ⇒ 尾部是孤儿，必须撤下（否则重放的增量会拼到旧文本后面）
    await waitFor(() => expect(useStreamStore.getState().tails.s1).toBeFalsy())
    unmount()
  })

  it('连接类提示在连接恢复后消失，业务提示保留', async () => {
    let calls = 0
    mock.read.mockImplementation(async (_sessionId, handlers: { onNotice: (name: string) => void }) => {
      calls += 1
      if (calls === 1) {
        handlers.onNotice('stream_overrun') // 连接类：恢复后该清掉
        handlers.onNotice('stream_rejected') // 会话被拒之类：重连也没用，保留
      }
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    const { result, unmount } = renderHook(() => useSessionStream('s1'), { wrapper })

    await waitFor(() => expect(mock.read).toHaveBeenCalledTimes(2), { timeout: 2000 })
    await waitFor(() => expect(result.current.notices).toEqual(['stream_rejected']))
    unmount()
  })

  it('一轮正常收尾不算断线：不显示"正在重连"', async () => {
    mock.read.mockResolvedValue(undefined) // 正常收尾 ⇒ 500ms 后重订阅（常规生命周期）
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )

    const { result, unmount } = renderHook(() => useSessionStream('s1'), { wrapper })

    await waitFor(() => expect(mock.read).toHaveBeenCalledTimes(2), { timeout: 2000 })
    expect(result.current.reconnecting).toBe(false)
    unmount()
  })

/**
 * 轮询一个异步状态机的取值，直到它等于 `expected`。
 *
 * 这里**不用 `waitFor`**：它把回调包在 `act()` 里，而这个 hook 的状态变化由"退避重连"的
 * `setTimeout` 驱动——实测直接轮询读取更稳（断言的是"最终出现过的取值"，不是渲染时机）。
 */
async function waitForFlag(
  read: () => boolean,
  expected: boolean,
  timeoutMs = 3_000,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (read() === expected) return
    await new Promise((resolve) => setTimeout(resolve, 50))
  }
  throw new Error(`等不到 ${expected ? 'true' : 'false'}`)
}

function renderStream() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const rendered = renderHook(() => useSessionStream('s1'), { wrapper })
  return { client, ...rendered }
}

describe('T72 断线期间的行为（§19.2）', () => {
  it('连不上时如实显示"正在重连"，连上后消失', async () => {
    // 恢复后订阅保持打开（不主动收尾）：这样 `connected` 才代表"正在接收"。
    mock.read.mockImplementation(() => new Promise(() => {}))
    // 断线一段时间（连续几次取快照都失败），再恢复
    mock.getSnapshot
      .mockRejectedValueOnce(new Error('network down'))
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue({ session_id: 's1', session_state: 'running', messages: [] })
    const { client, result, unmount } = renderStream()

    await waitForFlag(() => result.current.reconnecting, true)
    expect(result.current.connected).toBe(false)

    // 退避重连成功后清零，并拿到权威快照
    await waitForFlag(() => result.current.reconnecting, false)
    expect(result.current.connected).toBe(true)
    expect(client.getQueryData(queryKeys.snapshot('s1'))).toMatchObject({ session_state: 'running' })
    unmount()
  })

  it('断线期间产生的待审批：恢复后由权威快照呈现，且没有被自动决定', async () => {
    mock.read.mockResolvedValue(undefined)
    mock.getSnapshot
      .mockRejectedValueOnce(new Error('network down'))
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValue({ session_id: 's1', session_state: 'awaiting_approval', messages: [] })
    useStreamStore.setState({
      tails: { s1: { messageId: 'during-outage', modelCallId: null, text: '断线前的半句', sequence: 2 } },
    })
    const { client, result, unmount } = renderStream()

    await waitForFlag(() => result.current.reconnecting, true)
    await waitForFlag(() => result.current.reconnecting, false)

    // 审批只能由快照呈现；这里既没有"决定"的入口，也不保留断线前的临时拼接
    expect(client.getQueryData(queryKeys.snapshot('s1'))).toMatchObject({ session_state: 'awaiting_approval' })
    expect(useStreamStore.getState().tails.s1).toBeFalsy()
    unmount()
  })
})

})

