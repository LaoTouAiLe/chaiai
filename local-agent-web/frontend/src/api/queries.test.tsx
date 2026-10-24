import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useResolveApproval, useSubmitMessage } from './queries'
import { ApiError } from './client'

const mock = vi.hoisted(() => ({ submit: vi.fn(), resolve: vi.fn(), nextId: vi.fn() }))
vi.mock('./client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./client')>()),
  submitMessage: mock.submit,
  resolveApproval: mock.resolve,
  newRequestId: mock.nextId,
}))

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
}

beforeEach(() => {
  mock.submit.mockReset()
  mock.resolve.mockReset()
  mock.nextId.mockReset()
  let next = 0
  mock.nextId.mockImplementation(() => `req-${String(++next)}`)
})

describe('useSubmitMessage session boundary', () => {
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
