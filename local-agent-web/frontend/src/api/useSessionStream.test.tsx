import { CancelledError, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionStream } from './useSessionStream'
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
        handlers.onEvent({ event_type: eventType, payload: {} })
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
        event_type: 'message_started', payload: {
          role: 'assistant', message_id: 'a-1', model_call_id: 'call-1',
        }
      })
      handlers.onEvent({
        event_type: 'message_delta', payload: {
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
      handlers.onEvent({ event_type: 'task_failed', payload: {} })
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
})
