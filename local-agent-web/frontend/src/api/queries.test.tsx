import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useState, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useResolveApproval, useSideChatCreation, useSubmitMessage } from './queries'
import { ApiError } from './client'
import { onlineManager } from '@tanstack/react-query'

const mock = vi.hoisted(() => ({ submit: vi.fn(), resolve: vi.fn(), nextId: vi.fn(), createSideChat: vi.fn() }))
vi.mock('./client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./client')>()),
  submitMessage: mock.submit,
  resolveApproval: mock.resolve,
  newRequestId: mock.nextId,
  createSideChat: mock.createSideChat,
}))

it('attempts local side-chat requests while offline and rejects instead of queuing until reconnect', async () => {
  onlineManager.setOnline(false)
  mock.submit.mockRejectedValueOnce(new Error('offline request failed'))
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const localWrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const { result, unmount } = renderHook(() => useSubmitMessage('side', {}, 'always'), { wrapper: localWrapper })
  try {
    await act(async () => {
      await expect(result.current.mutateAsync({ text: 'offline draft' })).rejects.toThrow('offline request failed')
    })
    expect(mock.submit).toHaveBeenCalledOnce()
  } finally {
    onlineManager.setOnline(true)
    unmount()
    client.clear()
  }
})

function Wrapper({ children }: { children: ReactNode }) {
  const [client] = useState(() => new QueryClient())
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
const wrapper = Wrapper

beforeEach(() => {
  mock.submit.mockReset()
  mock.resolve.mockReset()
  mock.nextId.mockReset()
  mock.createSideChat.mockReset()
  let next = 0
  mock.nextId.mockImplementation(() => `req-${String(++next)}`)
})

describe('side chat creation lifecycle', () => {
  it('reuses pending and completed creation across pane unmounts until the tab records it', async () => {
    const client = new QueryClient()
    const stableWrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    let complete: (value: { session_id: string }) => void = () => {}
    mock.createSideChat.mockImplementationOnce(() => new Promise(resolve => { complete = resolve }))
    const first = renderHook(() => useSideChatCreation('main'), { wrapper: stableWrapper })
    const pending = first.result.current.create('sidechat-1')
    first.unmount()
    const second = renderHook(() => useSideChatCreation('main'), { wrapper: stableWrapper })
    expect(second.result.current.create('sidechat-1')).toBe(pending)
    complete({ session_id: 'side-1' })
    await expect(pending).resolves.toBe('side-1')
    expect(second.result.current.create('sidechat-1')).toBe(pending)
    expect(mock.createSideChat).toHaveBeenCalledOnce()
    second.result.current.forget('sidechat-1')
    mock.createSideChat.mockResolvedValueOnce({ session_id: 'side-2' })
    await expect(second.result.current.create('sidechat-1')).resolves.toBe('side-2')
    second.unmount()
    client.clear()
  })

  it('allows an explicit retry after creation fails', async () => {
    mock.createSideChat.mockRejectedValueOnce(new Error('offline'))
    mock.createSideChat.mockResolvedValueOnce({ session_id: 'side' })
    const { result } = renderHook(() => useSideChatCreation('main'), { wrapper })
    await expect(result.current.create('tab-1')).rejects.toThrow('offline')
    await expect(result.current.create('tab-1')).resolves.toBe('side')
    expect(mock.createSideChat).toHaveBeenCalledTimes(2)
  })

  it('does not let a closed creation failure discard a newer creation for that key', async () => {
    let rejectFirst: (error: Error) => void = () => {}
    mock.createSideChat.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
    mock.createSideChat.mockResolvedValueOnce({ session_id: 'new-side' })
    const { result } = renderHook(() => useSideChatCreation('main'), { wrapper })
    const first = result.current.create('tab-1').catch(error => error)
    result.current.forget('tab-1')
    const second = result.current.create('tab-1')
    rejectFirst(new Error('late failure'))
    await first
    expect(result.current.create('tab-1')).toBe(second)
    await expect(second).resolves.toBe('new-side')
    expect(mock.createSideChat).toHaveBeenCalledTimes(2)
  })

  it('isolates creation by tab and parent session', async () => {
    mock.createSideChat.mockResolvedValue({ session_id: 'side' })
    const { result, rerender } = renderHook(({ parent }) => useSideChatCreation(parent), { initialProps: { parent: 'main-a' }, wrapper })
    await result.current.create('tab-1')
    await result.current.create('tab-2')
    rerender({ parent: 'main-b' })
    await result.current.create('tab-1')
    expect(mock.createSideChat.mock.calls).toEqual([['main-a'], ['main-a'], ['main-b']])
  })
})

describe('useSubmitMessage session boundary', () => {
  it('keeps an uncertain request key and context after unmounting the sending pane', async () => {
    const client = new QueryClient()
    const stableWrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    let rejectFirst: (error: Error) => void = () => {}
    mock.submit.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject }))
    const first = renderHook(() => useSubmitMessage('side', { expected_snapshot_id: 'cp1' }), { wrapper: stableWrapper })
    let submitted: Promise<unknown>
    act(() => { submitted = first.result.current.mutateAsync({ text: '同一次请求' }).catch(error => error) })
    await waitFor(() => expect(mock.submit).toHaveBeenCalledOnce())
    first.unmount()
    await act(async () => { rejectFirst(new Error('response lost')); await submitted })
    mock.submit.mockResolvedValue({ status: 'completed' })
    const second = renderHook(() => useSubmitMessage('side', { expected_snapshot_id: 'cp2' }), { wrapper: stableWrapper })
    await act(async () => { await second.result.current.mutateAsync({ text: '同一次请求' }) })
    expect(mock.submit.mock.calls[1]?.slice(2)).toEqual(['req-1', { expected_snapshot_id: 'cp1' }])
    await act(async () => { await second.result.current.mutateAsync({ text: '同一次请求' }) })
    expect(mock.submit.mock.calls[2]?.[2]).toBe('req-2')
    second.unmount()
    client.clear()
  })

  it('does not share uncertain requests between QueryClients', async () => {
    mock.submit.mockRejectedValue(new Error('response lost'))
    const first = renderHook(() => useSubmitMessage('side'), { wrapper })
    await act(async () => { await expect(first.result.current.mutateAsync({ text: '同样文字' })).rejects.toThrow() })
    first.unmount()
    const second = renderHook(() => useSubmitMessage('side'), { wrapper })
    await act(async () => { await expect(second.result.current.mutateAsync({ text: '同样文字' })).rejects.toThrow() })
    expect(mock.submit.mock.calls.map(call => call[2])).toEqual(['req-1', 'req-2'])
  })

  it('does not clear a newer message retry key when an older response arrives late', async () => {
    let completeFirst: (value: unknown) => void = () => {}
    mock.submit.mockImplementationOnce(() => new Promise(resolve => { completeFirst = resolve }))
    mock.submit.mockRejectedValue(new Error('response lost'))
    const { result } = renderHook(() => useSubmitMessage('side'), { wrapper })
    let first: Promise<unknown>
    act(() => { first = result.current.mutateAsync({ text: '旧消息' }) })
    await waitFor(() => expect(mock.submit).toHaveBeenCalledOnce())
    await act(async () => { await expect(result.current.mutateAsync({ text: '新消息' })).rejects.toThrow() })
    await act(async () => { completeFirst({ status: 'completed' }); await first })
    await act(async () => { await expect(result.current.mutateAsync({ text: '新消息' })).rejects.toThrow() })
    expect(mock.submit.mock.calls.map(call => call[2])).toEqual(['req-1', 'req-2', 'req-2'])
  })

  it('binds workflow parameters to the retry key and assigns a new key after editing the scope', async () => {
    mock.submit.mockRejectedValue(new Error('network interrupted'))
    const { result, rerender } = renderHook(({ pattern }) => useSubmitMessage('s1', {
      workflow: { name: 'readonly_research', request: { pattern } },
    }), { initialProps: { pattern: 'docs/*.md' }, wrapper })
    act(() => result.current.mutate({ text: '核对原文' }))
    await waitFor(() => expect(result.current.isError).toBe(true))
    act(() => result.current.mutate({ text: '核对原文' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.isError).toBe(true))
    rerender({ pattern: 'src/*.py' })
    act(() => result.current.mutate({ text: '核对原文' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(3))
    expect(mock.submit.mock.calls.map((call) => call[2])).toEqual(['req-1', 'req-1', 'req-2'])
    expect(mock.submit.mock.calls[2]?.[3].workflow.request.pattern).toBe('src/*.py')
  })
  it('does not carry the previous session pending state or idempotency key into a new session', async () => {
    let completeFirst: (value: unknown) => void = () => undefined
    mock.submit.mockImplementationOnce(() => new Promise((resolve) => { completeFirst = resolve }))
    mock.submit.mockResolvedValueOnce({ status: 'completed' })
    const { result, rerender } = renderHook(({ id }) => useSubmitMessage(id), {
      initialProps: { id: 's1' }, wrapper,
    })
    act(() => result.current.mutate({ text: '同样的文字' }))
    await waitFor(() => expect(result.current.isPending).toBe(true))

    rerender({ id: 's2' })
    expect(result.current.isPending).toBe(false)
    act(() => result.current.mutate({ text: '同样的文字' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(2))
    expect(mock.submit.mock.calls.map((call) => [call[0], call[2]])).toEqual([
      ['s1', 'req-1'], ['s2', 'req-2'],
    ])
    act(() => completeFirst({ status: 'completed' }))
  })

  it('reuses the same id for an explicit retry of a failed submit in one session', async () => {
    mock.submit.mockRejectedValueOnce(new Error('network interrupted'))
    mock.submit.mockResolvedValueOnce({ status: 'completed' })
    const { result } = renderHook(() => useSubmitMessage('s1'), { wrapper })
    act(() => result.current.mutate({ text: '同一条' }))
    await waitFor(() => expect(result.current.isError).toBe(true))
    act(() => result.current.mutate({ text: '同一条' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(2))
    expect(mock.submit.mock.calls.map((call) => call[2])).toEqual(['req-1', 'req-1'])
  })
  it('keeps the original historical context after an uncertain network result', async () => {
    mock.submit.mockRejectedValueOnce(new Error('network interrupted'))
    mock.submit.mockResolvedValue({ status: 'completed' })
    const { result, rerender } = renderHook(({ checkpoint, branch }) => useSubmitMessage('s1', {
      parent_turn_id: 'u1', expected_branch_id: branch, expected_snapshot_id: checkpoint,
    }), { initialProps: { checkpoint: 'cp1', branch: 'main' }, wrapper })
    act(() => result.current.mutate({ text: '历史追问' }))
    await waitFor(() => expect(result.current.isError).toBe(true))
    rerender({ checkpoint: 'cp2', branch: 'main' })
    act(() => result.current.mutate({ text: '历史追问' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(2))
    expect(mock.submit.mock.calls[1]?.slice(2)).toEqual(['req-1', {
      parent_turn_id: 'u1', expected_branch_id: 'main', expected_snapshot_id: 'cp1',
    }])
    await waitFor(() => expect(result.current.isPending).toBe(false))
    rerender({ checkpoint: 'cp3', branch: 'b1' })
    act(() => result.current.mutate({ text: '历史追问' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(3))
    expect(mock.submit.mock.calls[2]?.[2]).toBe('req-2')
  })
  it('uses a refreshed context only after an explicit path rejection before execution', async () => {
    mock.submit.mockRejectedValueOnce(new ApiError({ status: 409, code: 'CONVERSATION_CONFLICT', message: '路径已改变' }))
    mock.submit.mockResolvedValue({ status: 'completed' })
    const { result, rerender } = renderHook(({ checkpoint }) => useSubmitMessage('s1', {
      expected_branch_id: 'main', expected_snapshot_id: checkpoint,
    }), { initialProps: { checkpoint: 'cp1' }, wrapper })
    act(() => result.current.mutate({ text: '追问' }))
    await waitFor(() => expect(result.current.isError).toBe(true))
    rerender({ checkpoint: 'cp2' })
    act(() => result.current.mutate({ text: '追问' }))
    await waitFor(() => expect(mock.submit).toHaveBeenCalledTimes(2))
    expect(mock.submit.mock.calls[1]?.slice(2)).toEqual(['req-2', {
      expected_branch_id: 'main', expected_snapshot_id: 'cp2',
    }])
  })
})

describe('useResolveApproval idempotency and authority', () => {
  it('reuses one key for a retry of the same decision and a new one for the other decision', async () => {
    mock.resolve.mockRejectedValueOnce(new Error('session busy'))
    mock.resolve.mockResolvedValue({ status: 'completed' })
    const { result } = renderHook(() => useResolveApproval('s1'), { wrapper })

    act(() => result.current.resolve('ap-1', 'reject'))
    await waitFor(() => expect(result.current.error).not.toBeNull())
    // 同一次决定的显式重试必须复用同一个键，否则服务端会当成第二个决定。
    act(() => result.current.resolve('ap-1', 'reject'))
    await waitFor(() => expect(mock.resolve).toHaveBeenCalledTimes(2))
    // 换一个决定就是另一次决定，必须换键。
    act(() => result.current.resolve('ap-1', 'approve'))
    await waitFor(() => expect(mock.resolve).toHaveBeenCalledTimes(3))

    expect(mock.resolve.mock.calls.map((call) => [call[1], call[2].decision, call[2].client_request_id])).toEqual([
      ['ap-1', 'reject', 'req-1'],
      ['ap-1', 'reject', 'req-1'],
      ['ap-1', 'approve', 'req-2'],
    ])
  })

  it('refetches the snapshot after a failed decision because the server stays the authority', async () => {
    const client = new QueryClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    const localWrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    )
    mock.resolve.mockRejectedValueOnce(new Error('session busy'))
    const { result } = renderHook(() => useResolveApproval('s1'), { wrapper: localWrapper })

    act(() => result.current.resolve('ap-1', 'approve'))
    await waitFor(() => expect(result.current.error).not.toBeNull())
    // 失败也要重取：过期或被别处抢先处理，只能由服务端说清楚。
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['snapshot', 's1'] })
  })
})
