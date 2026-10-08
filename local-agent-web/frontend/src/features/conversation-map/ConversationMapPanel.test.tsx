import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../../api/schema.gen'
import { shellScenario } from '../../fixtures/scenarios'
import { ConversationMapPanel } from './ConversationMapPanel'
import { ancestry, flattenMap, mapConversationMap } from './adapters'
import { arrangeBranch, getConversationMap, getTurnSnapshot, selectBranch } from '../../api/client'

vi.mock('../../api/client', async (original) => ({
  ...await original<typeof import('../../api/client')>(),
  getConversationMap: vi.fn(), getTurnSnapshot: vi.fn(), selectBranch: vi.fn(), arrangeBranch: vi.fn(),
}))
const tree: components['schemas']['ConversationMapView'] = {
  session_id: 's1', active_branch_id: 'main', matched_turn_ids: [],
  branches: [{ branch_id: 'main', title: '原讨论', position: 0, active: true, tip_turn_id: 'u3' },
    { branch_id: 'b1', title: '另一种方案', position: 1, active: false, tip_turn_id: 'u4', fork_turn_id: 'u1' }],
  nodes: [
    { turn_id: 'u1', parent_turn_id: null, origin_branch_id: 'main', ordinal: 1, question: '分析缓存', answer_preview: '先检查前缀', state: 'completed', can_continue: true },
    { turn_id: 'u2', parent_turn_id: 'u1', origin_branch_id: 'main', ordinal: 2, question: '原优化方案', answer_preview: '原答案', state: 'completed', can_continue: true },
    { turn_id: 'u3', parent_turn_id: 'u2', origin_branch_id: 'main', ordinal: 3, question: '原方案验证', answer_preview: '验证答案', state: 'completed', can_continue: true },
    { turn_id: 'u4', parent_turn_id: 'u1', origin_branch_id: 'b1', ordinal: 4, question: '另一种方案验证', answer_preview: '分支答案', state: 'completed', can_continue: true },
  ],
}
const snapshot: components['schemas']['SessionSnapshot'] = {
  mode: 'agent',
  schema_version: 1,
  session_id: 's1', snapshot_id: 'cp1', title: '测试', branch_id: 'main', branch_title: '原讨论',
  workspace: { workspace_id: 'w1', display_name: '工作区' }, session_state: 'completed',
  created_at: '2026-10-07T00:00:00Z',
  messages: [{ kind: 'user', message_id: 'u1', text: '分析缓存' }, { kind: 'assistant', message_id: 'a1', text: '该轮独立回答', complete: true, chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text' }],
}
function mount(busy = false) {
  const callbacks = { onContinue: vi.fn(), onLocate: vi.fn(), onBranchChanged: vi.fn() }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const view = (snapshotId: string) => <QueryClientProvider client={client}><ConversationMapPanel sessionId="s1" scenario={shellScenario}
    busy={busy} snapshotId={snapshotId} {...callbacks} /></QueryClientProvider>
  const rendered = render(view('cp1'))
  return { ...callbacks, changeSnapshot: (id: string) => rendered.rerender(view(id)) }
}
beforeEach(() => {
  vi.mocked(getConversationMap).mockReset().mockResolvedValue(tree)
  vi.mocked(getTurnSnapshot).mockReset().mockResolvedValue(snapshot)
  vi.mocked(selectBranch).mockReset().mockResolvedValue(snapshot)
  vi.mocked(arrangeBranch).mockReset().mockResolvedValue(tree)
})
describe('会话地图', () => {
  it('preserves ancestry and changes only sibling display order', () => {
    const data = mapConversationMap(tree)
    expect([...ancestry(data.nodes, 'u4')]).toEqual(['u4', 'u1'])
    expect(flattenMap(data, new Set(), false).map((n) => n.node.id)).toEqual(['u1', 'u2', 'u3', 'u4'])
    data.branches[1]!.position = -1
    expect(flattenMap(data, new Set(), false).map((n) => n.node.id)).toEqual(['u1', 'u4', 'u2', 'u3'])
    expect(data.nodes[3]!.parentId).toBe('u1')
  })
  it('loads an independent turn and prepares its ancestor context only on explicit followup', async () => {
    const callbacks = mount()
    fireEvent.click(await screen.findByRole('button', { name: /第1轮 · 已完成/ }))
    expect(await screen.findByText('该轮独立回答')).toBeInTheDocument()
    expect(callbacks.onContinue).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '基于此轮继续提问' }))
    await waitFor(() => expect(getTurnSnapshot).toHaveBeenCalledWith('s1', 'u1', true))
    await waitFor(() => expect(callbacks.onContinue).toHaveBeenCalledWith(expect.objectContaining({ id: 'u1' }), snapshot))
  })
  it('searches questions and answers through the server', async () => {
    mount()
    await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    fireEvent.change(screen.getByRole('textbox', { name: '搜索会话问答' }), { target: { value: '前缀' } })
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))
    await waitFor(() => expect(getConversationMap).toHaveBeenCalledWith('s1', '前缀'))
  })
  it('shows recorded model calls even when a turn did not use tools', async () => {
    vi.mocked(getTurnSnapshot).mockResolvedValue({ ...snapshot, model_calls: [{
      model_call_id: 'call1', run_id: 'r1', purpose: 'chat', connection_id: 'c1',
      config_version: 'v1', provider: 'test', model_id: 'test-model',
      started_at: '2026-10-07T00:00:00Z', status: 'ok',
      cache_metrics_reported: false, actual_uncached_input_tokens: 123, actual_output_tokens: 12,
    }] })
    mount()
    fireEvent.click(await screen.findByRole('button', { name: /第1轮 · 已完成/ }))
    fireEvent.click(screen.getByRole('button', { name: '执行链路' }))
    expect(await screen.findByText('闲聊回答 · test-model')).toBeInTheDocument()
    expect(screen.getByText(/输入 123 · 输出 12/)).toBeInTheDocument()
    expect(screen.getByText('本轮没有工具操作记录。')).toBeInTheDocument()
  })
  it('blocks followup and branch switching while a run or approval is active', async () => {
    mount(true)
    fireEvent.click(await screen.findByRole('button', { name: /第1轮 · 已完成/ }))
    expect(screen.getByRole('button', { name: '基于此轮继续提问' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: '当前分支' })).toBeDisabled()
  })
  it('sends only branch title and display position when arranging', async () => {
    mount()
    await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    fireEvent.click(screen.getByRole('button', { name: '另一种方案上移' }))
    await waitFor(() => expect(arrangeBranch).toHaveBeenCalledWith('s1', 'b1', { title: '另一种方案', position: 0 }))
  })
  it('starts with the full canvas and closes details without changing the conversation path', async () => {
    const callbacks = mount()
    const card = await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    expect(screen.queryByRole('complementary', { name: '问答详情' })).not.toBeInTheDocument()
    expect(getTurnSnapshot).not.toHaveBeenCalled()
    fireEvent.click(card)
    expect(await screen.findByText('该轮独立回答')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭问答详情' }))
    expect(screen.queryByRole('complementary', { name: '问答详情' })).not.toBeInTheDocument()
    expect(card).toHaveFocus()
    expect(callbacks.onBranchChanged).not.toHaveBeenCalled()
    expect(selectBranch).not.toHaveBeenCalled()
  })
  it('reveals the current branch tip after its ancestors were collapsed', async () => {
    mount()
    await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    fireEvent.click(screen.getByRole('button', { name: '折叠第1轮后续' }))
    expect(screen.queryByRole('button', { name: /第3轮 · 已完成/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '定位当前分支' }))
    expect(await screen.findByRole('button', { name: /第3轮 · 已完成/ })).toBeInTheDocument()
  })
  it('zooms and pans the viewport without changing node positions or branch state', async () => {
    mount()
    await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    const world = document.querySelector<HTMLElement>('[data-map-transform]')!
    const root = document.querySelector<HTMLElement>('[data-map-node="u1"]')!
    const position = root.style.cssText
    const initial = world.style.transform
    fireEvent.click(screen.getByRole('button', { name: '放大地图' }))
    expect(world.style.transform).not.toBe(initial)
    const zoomed = world.style.transform
    fireEvent.keyDown(screen.getByRole('region', { name: '树状地图画布' }), { key: 'ArrowLeft' })
    expect(world.style.transform).not.toBe(zoomed)
    expect(root.style.cssText).toBe(position)
    expect(selectBranch).not.toHaveBeenCalled()
  })
  it('keeps the canvas mounted and its viewport when a stream snapshot starts a refresh', async () => {
    const mounted = mount()
    await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    fireEvent.keyDown(screen.getByRole('region', { name: '树状地图画布' }), { key: 'ArrowLeft' })
    const world = document.querySelector<HTMLElement>('[data-map-transform]')!
    const transform = world.style.transform
    vi.mocked(getConversationMap).mockImplementationOnce(() => new Promise(() => undefined))
    mounted.changeSnapshot('cp2')
    expect(document.querySelector('[data-map-transform]')).toBe(world)
    expect(world.style.transform).toBe(transform)
  })
  it('closes the detail drawer with Escape while the card still has focus', async () => {
    mount()
    const card = await screen.findByRole('button', { name: /第1轮 · 已完成/ })
    fireEvent.click(card)
    expect(await screen.findByText('该轮独立回答')).toBeInTheDocument()
    fireEvent.keyDown(card, { key: 'Escape' })
    expect(screen.queryByRole('complementary', { name: '问答详情' })).not.toBeInTheDocument()
  })
})
