import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { components } from '../../api/schema.gen'
import { getMemoryPage, getUsagePage } from '../../api/client'
import { DataCenter } from './DataCenter'
import { countLabel, inputTokens, percentLabel, utcRange } from './adapters'

vi.mock('../../api/client', async (original) => ({
  ...await original<typeof import('../../api/client')>(), getMemoryPage: vi.fn(), getUsagePage: vi.fn(),
}))
const entry: components['schemas']['MemoryEntryView'] = {
  id: 'm1', kind: 'fact', title: '项目语言', content: '使用 Python 和 pytest', scope: 'project', status: 'active',
  source: '测试来源', confidence: .9, truncated: false,
}
const memory: components['schemas']['MemoryPageView'] = {
  entries: [entry], total: 1, offset: 0, limit: 30, counts: { fact: 1 }, incomplete: false,
  resources: [{ kind: 'medium', enabled: false, state: 'missing', note: '尚无记忆库。' },
    { kind: 'long', enabled: false, state: 'available', note: '仅查看保留数据。' }],
}
const call: components['schemas']['UsageCallView'] = {
  session_id: 's1', session_title: '测试会话', workspace_id: 'w1', call: {
    model_call_id: 'c1', run_id: 'r1', purpose: 'chat', model_id: 'test-model', connection_id: 'conn', config_version: 'v1',
    provider: 'fake', status: 'ok', started_at: '2026-10-07T03:00:00Z', actual_output_tokens: 0, cache_metrics_reported: false,
  },
}
const usage: components['schemas']['UsagePageView'] = {
  totals: { calls: 1, missing_usage_calls: 1, failed_calls: 0 }, daily: [{ label: '2026-10-07', totals: { calls: 1, failed_calls: 0, missing_usage_calls: 1 } }],
  models: [{ label: 'test-model', totals: { calls: 1, failed_calls: 0, missing_usage_calls: 1 } }], purposes: [], entries: [call], total: 1, offset: 0, limit: 30,
  scanned_sessions: 1, missing_record_sessions: 0, incomplete: false,
  generated_at: '2026-10-07T03:00:00Z',
}
function mount() {
  const onClose = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><DataCenter initialWorkspaceId="w1" running={false}
    workspaces={[{ workspace_id: 'w1', display_name: '项目一' }, { workspace_id: 'w2', display_name: '项目二' }]}
    onClose={onClose} /></QueryClientProvider>)
  return { onClose }
}
beforeEach(() => {
  vi.mocked(getMemoryPage).mockReset().mockResolvedValue(memory)
  vi.mocked(getUsagePage).mockReset().mockResolvedValue(usage)
})
describe('数据中台', () => {
  it('shows unknown usage honestly and retained memories independently of enablement', async () => {
    const { onClose } = mount()
    await screen.findByRole('button', { name: 'test-model 1 次' })
    expect(screen.getAllByText('未完整上报').length).toBeGreaterThan(0)
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
    expect(screen.getAllByText('召回已关闭')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: '返回会话' }))
    expect(onClose).toHaveBeenCalledOnce()
  })
  it('filters a clicked trend day using UTC exclusive upper bound', async () => {
    mount()
    fireEvent.click(await screen.findByRole('button', { name: '10-07 1 次' }))
    await waitFor(() => expect(getUsagePage).toHaveBeenLastCalledWith(expect.objectContaining({
      since: '2026-10-07T00:00:00Z', until: '2026-10-08T00:00:00.000Z', offset: 0,
    })))
    expect(screen.getByRole('button', { name: 'Token 用量' })).toHaveAttribute('aria-current', 'page')
  })
  it('searches and scopes memory on the server and closes old detail when filters change', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: '长期事实' }))
    fireEvent.click(await screen.findByRole('button', { name: '项目语言 使用 Python 和 pytest' }))
    expect(screen.getByRole('complementary', { name: '数据详情' })).toHaveTextContent('测试来源')
    fireEvent.change(screen.getByRole('textbox', { name: '搜索记忆' }), { target: { value: 'pytest' } })
    fireEvent.click(screen.getByRole('button', { name: '搜索' }))
    await waitFor(() => expect(getMemoryPage).toHaveBeenLastCalledWith(expect.objectContaining({ kind: 'fact', query: 'pytest' })))
    expect(screen.queryByRole('complementary', { name: '数据详情' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: '记忆工作区' }), { target: { value: 'w2' } })
    await waitFor(() => expect(getMemoryPage).toHaveBeenLastCalledWith(expect.objectContaining({ workspace_id: 'w2', offset: 0 })))
  })
  it('paginates without counting only the current page and restores focus after closing', async () => {
    vi.mocked(getMemoryPage).mockImplementation(async (filters) => ({ ...memory, total: 31,
      entries: [{ ...entry, title: filters.offset === 30 ? '第二页' : '第一页' }] }))
    mount()
    fireEvent.click(screen.getByRole('button', { name: '长期事实' }))
    const row = await screen.findByRole('button', { name: '第一页 使用 Python 和 pytest' })
    fireEvent.click(row)
    fireEvent.click(screen.getByRole('button', { name: '关闭数据详情' }))
    expect(row).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    await screen.findByRole('button', { name: '第二页 使用 Python 和 pytest' })
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
    expect(getMemoryPage).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 30 }))
  })
  it('opens call detail, displays a real zero, and closes on Escape', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Token 用量' }))
    fireEvent.click(await screen.findByRole('button', { name: 'test-model 测试会话' }))
    const detail = screen.getByRole('complementary', { name: '数据详情' })
    expect(detail).toHaveTextContent('闲聊回答')
    expect(detail).toHaveTextContent('未完整上报')
    expect(detail).toHaveTextContent('输出 Token0')
    fireEvent.keyDown(detail, { key: 'Escape' })
    expect(screen.queryByRole('complementary', { name: '数据详情' })).not.toBeInTheDocument()
  })
  it('rejects reversed dates locally without sending an invalid query', async () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Token 用量' }))
    await screen.findByRole('button', { name: 'test-model 测试会话' })
    fireEvent.change(screen.getByLabelText('开始日期 UTC'), { target: { value: '2026-10-09' } })
    await waitFor(() => expect(getUsagePage).toHaveBeenLastCalledWith(expect.objectContaining({ since: '2026-10-09T00:00:00Z' })))
    const count = vi.mocked(getUsagePage).mock.calls.length
    fireEvent.change(screen.getByLabelText('结束日期 UTC'), { target: { value: '2026-10-01' } })
    expect(screen.getByRole('alert')).toHaveTextContent('开始日期不能晚于结束日期')
    expect(getUsagePage).toHaveBeenCalledTimes(count)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '长期事实' }))
    await screen.findByRole('button', { name: '项目语言 使用 Python 和 pytest' })
    expect(screen.getByRole('button', { name: '刷新数据' })).toBeEnabled()
  })
  it('reports network failure and refreshes instead of claiming an empty store', async () => {
    vi.mocked(getMemoryPage).mockRejectedValueOnce(new Error('offline'))
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent('数据暂时无法读取')
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新数据' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '刷新数据' }))
    await screen.findByText('仅查看保留数据。')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
  it('distinguishes unknown from zero and handles UTC year rollover', () => {
    expect(countLabel(null)).toBe('未完整上报')
    expect(countLabel(0)).toBe('0')
    expect(percentLabel(0)).toBe('0.0%')
    expect(inputTokens(call)).toBeNull()
    expect(utcRange('2026-12-31', '2026-12-31')).toEqual({ since: '2026-12-31T00:00:00Z', until: '2027-01-01T00:00:00.000Z' })
  })
})
