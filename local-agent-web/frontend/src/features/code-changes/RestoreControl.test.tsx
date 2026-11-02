// 还原控件的交互契约（2026-10-29）。
//
// 这里钉住的是**用户可见的承诺**，不是实现：
// 1. 点「还原」先出确认，确认里必须写明"对话记录不会回退"——用户最怕的是把聊天也退没了；
// 2. 基准点由用户选（照 hermes 的「显式选 checkpoint」做法）：两个时默认第一个，
//    选了哪个就按哪个粒度发请求；
// 3. 先不带 force 发一次；只有服务端说"被改动过"，才给「仍然覆盖」；
// 4. 结果要分三区说清（成功 / 跳过 / 失败），跳过必须带原因。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { RestoreControl } from './RestoreControl'
import type { RestoreAction } from './RestoreControl'

const mock = vi.hoisted(() => ({ restore: vi.fn() }))
vi.mock('../../api/client', async original => ({
  ...await original<typeof import('../../api/client')>(),
  restoreSessionChanges: mock.restore,
}))

interface ResultOverrides {
  restored?: { path: string; action: string; basis: string; undo_ref: string | null }[]
  skipped?: { path: string; reason: string }[]
  failed?: { path: string; error: string }[]
}

function payload({ restored = [], skipped = [], failed = [] }: ResultOverrides = {}) {
  return {
    scope: 'turn' as const,
    run_id: 'r1',
    restored,
    skipped,
    failed,
    changes: {
      session_id: 's1', turns: [], total_files: 0, additions: 0, deletions: 0,
      unrecorded: [], unrecorded_note: null,
    },
  }
}

const turnOnly: RestoreAction[] = [{
  label: '本轮开始前',
  note: '把这 2 个文件退回到本轮开始前的样子',
  scope: 'turn',
  runId: 'r1',
}]

const twoBasels: RestoreAction[] = [
  { label: '会话开始前', note: '退回本会话开始前', scope: 'file', path: 'a.py' },
  { label: '本轮开始前', note: '只撤本轮改动', scope: 'turn', runId: 'r-latest', paths: ['a.py'] },
]

function host(actions: RestoreAction[] = turnOnly) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <RestoreControl sessionId="s1" label="还原" actions={actions} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  mock.restore.mockReset()
})

describe('还原控件', () => {
  it('明确显示还原时已删除本次新建的文件', async () => {
    mock.restore.mockResolvedValue(payload({
      restored: [{ path: 'new.py', action: 'removed', basis: 'turn', undo_ref: 'undo/fake' }],
    }))
    host()
    fireEvent.click(screen.getByRole('button', { name: '还原' }))
    fireEvent.click(screen.getByRole('button', { name: '确认还原' }))
    expect(await screen.findByText(/已删除本次新建的文件/)).toBeVisible()
  })
  it('覆盖重试失败时保留结果和错误，再次重试仍使用相同基准', async () => {
    mock.restore.mockResolvedValueOnce(payload({ skipped: [{ path: 'a.py', reason: '又被改动过' }] }))
      .mockRejectedValueOnce(new Error('disk failure'))
      .mockResolvedValueOnce(payload({ restored: [{ path: 'a.py', action: 'written', basis: 'turn', undo_ref: null }] }))
    host()
    fireEvent.click(screen.getByRole('button', { name: '还原' }))
    fireEvent.click(screen.getByRole('button', { name: '确认还原' }))
    fireEvent.click(await screen.findByRole('button', { name: '仍然覆盖被跳过的文件' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('这次还原没有完成')
    expect(screen.getByText(/1 个跳过/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '仍然覆盖被跳过的文件' }))
    expect(await screen.findByText(/已写回/)).toBeInTheDocument()
    expect(mock.restore.mock.calls[2][1]).toMatchObject({ run_id: 'r1', force: true })
  })
  it('先出确认，并明确"对话记录不会回退"；此时不发请求', () => {
    host()
    fireEvent.click(screen.getByRole('button', { name: '还原' }))

    expect(screen.getByText(/对话记录不会回退/)).toBeTruthy()
    expect(screen.getByText(/把这 2 个文件退回到本轮开始前/)).toBeTruthy()
    expect(mock.restore).not.toHaveBeenCalled()
  })

  it('确认后按 turn 粒度发一次，默认不带 force', async () => {
    mock.restore.mockResolvedValue(payload({
      restored: [{ path: 'a.py', action: 'written', basis: 'turn', undo_ref: null }],
    }))
    host()
    fireEvent.click(screen.getByRole('button', { name: '还原' }))
    fireEvent.click(screen.getByRole('button', { name: '确认还原' }))

    await waitFor(() => expect(mock.restore).toHaveBeenCalledTimes(1))
    expect(mock.restore.mock.calls[0]).toEqual([
      's1',
      { scope: 'turn', path: undefined, run_id: 'r1', paths: undefined, force: false },
    ])
    expect(await screen.findByText(/1 个处理成功/)).toBeTruthy()
    expect(screen.getByText(/已写回/)).toBeTruthy()
  })

  it('两个基准时默认第一个（会话开始前），切到第二个后请求换成 turn 粒度', async () => {
    mock.restore.mockResolvedValue(payload({
      restored: [{ path: 'a.py', action: 'written', basis: 'turn', undo_ref: null }],
    }))
    host(twoBasels)
    fireEvent.click(screen.getByRole('button', { name: '还原' }))

    const radios = screen.getAllByRole('radio') as HTMLInputElement[]
    expect(radios).toHaveLength(2)
    expect(radios[0].checked).toBe(true)

    fireEvent.click(radios[1])
    fireEvent.click(screen.getByRole('button', { name: '确认还原' }))

    await waitFor(() => expect(mock.restore).toHaveBeenCalledTimes(1))
    expect(mock.restore.mock.calls[0][1]).toMatchObject({
      scope: 'turn', run_id: 'r-latest', paths: ['a.py'], force: false,
    })
  })

  it('被跳过的文件要给出原因；只有"被改动过"才提供「仍然覆盖」', async () => {
    mock.restore.mockResolvedValue(payload({
      skipped: [{ path: 'b.py', reason: '这个文件在 AI 改完之后又被改动过' }],
    }))
    host()
    fireEvent.click(screen.getByRole('button', { name: '还原' }))
    fireEvent.click(screen.getByRole('button', { name: '确认还原' }))

    expect(await screen.findByText(/1 个跳过/)).toBeTruthy()
    expect(screen.getByText(/又被改动过/)).toBeTruthy()

    mock.restore.mockResolvedValue(payload({
      restored: [{ path: 'b.py', action: 'written', basis: 'turn', undo_ref: 'abc' }],
    }))
    fireEvent.click(screen.getByRole('button', { name: '仍然覆盖被跳过的文件' }))

    await waitFor(() => expect(mock.restore).toHaveBeenCalledTimes(2))
    expect(mock.restore.mock.calls[1][1]).toMatchObject({ scope: 'turn', run_id: 'r1', force: true })
    expect(await screen.findByText(/1 个处理成功/)).toBeTruthy()
  })

  it('非冲突原因的跳过不提供覆盖按钮（比如没有改前内容）', async () => {
    mock.restore.mockResolvedValue(payload({
      skipped: [{ path: 'big.bin', reason: '二进制文件，未保存改前内容' }],
    }))
    host()
    fireEvent.click(screen.getByRole('button', { name: '还原' }))
    fireEvent.click(screen.getByRole('button', { name: '确认还原' }))

    expect(await screen.findByText(/1 个跳过/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: '仍然覆盖被跳过的文件' })).toBeNull()
  })
})
