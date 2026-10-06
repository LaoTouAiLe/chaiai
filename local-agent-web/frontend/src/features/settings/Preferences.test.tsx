import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'
import type { PreferencesDto } from '../../api/client'
import { Composer } from '../../components/Composer'
import { emptySessionScenario } from '../../fixtures/scenarios'
import { ReasoningRow } from '../conversation/ReasoningRow'
import { PreferenceSection } from './PreferenceSection'
import { PreferencesProvider } from './PreferencesProvider'
import { GeneralContext, generalDefaults, preferencesKey } from './preferencesContext'

const mock = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), test: vi.fn() }))
vi.mock('../../api/client', async original => ({
  ...await original<typeof import('../../api/client')>(), getPreferences: mock.get, savePreferences: mock.save, testMemoryPreferences: mock.test,
}))
function fixture(): PreferencesDto {
  const values = {
    general: { ...generalDefaults },
    execution: { max_tool_loops: 20, max_retries_per_step: 2, shell_timeout_seconds: 60, shell_max_output_chars: 30000 },
    retrieval: { enabled: true, max_files: 2000, max_results: 8, timeout_seconds: 10, context_lines: 20 },
    memory: { enabled: true, confidence_threshold: .75, importance_threshold: .5, aging_enabled: true, forget_after_days: 180 },
    medium_term: { enabled: false, top_k: 8, min_score: .53, decay_half_life_days: 30 },
    embedding: { enabled: false, provider: 'onnx_local' as const, model: 'unit-test', cache_dir: 'E:/models', local_files_only: true, base_url: '', api_key_env: '', allow_plaintext_http: false },
  }
  return { version: 0, writable: true, values, defaults: structuredClone(values), advanced: { compression_trigger_ratio: .8 } }
}
function host(node: ReactNode, data = fixture()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(preferencesKey, data)
  if (!mock.get.getMockImplementation()) mock.get.mockResolvedValue(data)
  return { ...render(<QueryClientProvider client={client}>{node}</QueryClientProvider>), client }
}
beforeEach(() => { mock.get.mockReset(); mock.save.mockReset(); mock.test.mockReset(); vi.unstubAllGlobals() })

describe('应用偏好', () => {
  it('shows pending local cache restart after save instead of claiming immediate task activation', async () => {
    const data = fixture()
    data.resources = { backend: 'chroma', path: 'data/chroma', collection: 'agent_memory', vector_available: true, vector_initialized: true, embedding_state: 'ready', embedding_detail: '具备条件', key_configured: false, restart_required: false }
    mock.save.mockImplementation(async values => ({ ...data, values, version: 1, resources: { ...data.resources, embedding_state: 'not_ready', embedding_detail: '新缓存目录需重启后端生效', restart_required: true } }))
    mock.test.mockResolvedValue({ state: 'not_ready', detail: '请保存后重启后端，再次测试', error_category: 'restart_required' })
    host(<PreferenceSection group="embedding" />, data)
    fireEvent.change(screen.getByRole('textbox', { name: '模型缓存目录' }), { target: { value: 'E:/new-models' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByText('已保存，新缓存目录需重启后端生效')
    expect(screen.queryByText('已保存，下一次新任务生效')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('重启后测试')
    fireEvent.click(screen.getByRole('button', { name: '测试当前配置' }))
    await screen.findByText(/测试未通过.*请保存后重启后端/)
  })

  it('saves medium memory thresholds with the original version and retains an invalid draft', async () => {
    mock.save.mockRejectedValue(new Error('启用中期记忆前，请先启用 Embedding'))
    host(<PreferenceSection group="medium_term" />)
    fireEvent.click(screen.getByRole('checkbox', { name: '启用中期记忆' }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '最低语义相似度' }), { target: { value: '.555' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByRole('alert')
    expect(mock.save.mock.calls[0][0].medium_term.min_score).toBe(.555)
    expect(mock.save.mock.calls[0][1]).toBe(0)
    expect(screen.getByRole('checkbox', { name: '启用中期记忆' })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
    expect(screen.getByRole('checkbox', { name: '启用中期记忆' })).not.toBeChecked()
  })

  it('switches embedding providers without sending foreign fields, tests a draft without saving, and resets the result on edit', async () => {
    const data = fixture()
    data.resources = { backend: 'chroma', path: 'data/chroma', collection: 'agent_memory', vector_available: true, vector_initialized: false, embedding_state: 'disabled', embedding_detail: '未启用', key_configured: false, restart_required: false }
    mock.test.mockResolvedValue({ state: 'passed', detail: '向量生成成功', dimension: 3 })
    mock.save.mockImplementation(async values => ({ ...data, values, version: 1 }))
    host(<PreferenceSection group="embedding" />, data)
    expect(mock.test).not.toHaveBeenCalled()
    expect(screen.getByText('agent_memory')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: '数据目录（只读）' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: '向量生成方式' }), { target: { value: 'openai_compatible' } })
    expect(screen.queryByRole('textbox', { name: '模型缓存目录' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: '启用 Embedding' }))
    fireEvent.change(screen.getByRole('textbox', { name: '向量模型名称' }), { target: { value: 'api-model' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Embedding API 端点' }), { target: { value: 'https://example.com/v1' } })
    fireEvent.click(screen.getByRole('button', { name: '测试当前配置' }))
    await screen.findByText(/测试通过/)
    expect(mock.save).not.toHaveBeenCalled()
    expect(mock.test.mock.calls[0][0].embedding).toMatchObject({ model: 'api-model', cache_dir: '', local_files_only: false, api_key_env: 'EMBEDDING_API_KEY' })
    expect(mock.test.mock.calls[0][1]).toBe(0)
    fireEvent.change(screen.getByRole('textbox', { name: '向量模型名称' }), { target: { value: 'another-model' } })
    expect(screen.queryByText(/测试通过/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByText('已保存，下一次新任务生效')
    expect(mock.save.mock.calls[0][0].embedding.model).toBe('another-model')
    fireEvent.change(screen.getByRole('combobox', { name: '向量生成方式' }), { target: { value: 'onnx_local' } })
    expect(screen.queryByRole('textbox', { name: 'API Key 环境变量名' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '模型缓存目录' })).toBeInTheDocument()
  })

  it('accepts configured decimal thresholds while saving another memory option', async () => {
    const data = fixture()
    data.values.memory.confidence_threshold = .755
    data.values.memory.importance_threshold = .503
    mock.save.mockImplementation(async values => ({ ...data, values, version: 1 }))
    host(<PreferenceSection group="memory" />, data)
    const confidence = screen.getByRole('spinbutton', { name: '置信度阈值' }) as HTMLInputElement
    const importance = screen.getByRole('spinbutton', { name: '重要性阈值' }) as HTMLInputElement
    expect(confidence.checkValidity()).toBe(true)
    expect(importance.checkValidity()).toBe(true)
    fireEvent.click(screen.getByRole('checkbox', { name: '启用长期记忆' }))
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByRole('status')
    expect(mock.save).toHaveBeenCalledWith({ ...data.values, memory: { ...data.values.memory, enabled: false } }, 0)
    fireEvent.change(confidence, { target: { value: '1.001' } })
    expect(confidence.checkValidity()).toBe(false)
    fireEvent.change(confidence, { target: { value: '-0.001' } })
    expect(confidence.checkValidity()).toBe(false)
  })

  it('reloads current settings on reopening even while the global provider stays mounted', async () => {
    const data = fixture()
    const { client, rerender } = host(<PreferencesProvider><PreferenceSection group="execution" /></PreferencesProvider>, data)
    await waitFor(() => expect(mock.get).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(client.isFetching()).toBe(0))
    rerender(<QueryClientProvider client={client}><PreferencesProvider><span>设置已关闭</span></PreferencesProvider></QueryClientProvider>)
    const fresh = fixture()
    fresh.version = 1
    fresh.values.execution.max_tool_loops = 28
    fresh.values.general = { ...generalDefaults, theme: 'dark' }
    mock.get.mockResolvedValue(fresh)
    mock.save.mockImplementation(async values => ({ ...fresh, values, version: 2 }))
    rerender(<QueryClientProvider client={client}><PreferencesProvider><PreferenceSection group="execution" /></PreferencesProvider></QueryClientProvider>)
    await waitFor(() => expect(screen.getByRole('spinbutton', { name: '每轮工具循环上限' })).toHaveValue(28))
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(mock.get).toHaveBeenCalledTimes(2)
    fireEvent.change(screen.getByRole('spinbutton', { name: '每轮工具循环上限' }), { target: { value: '29' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByRole('status')
    expect(mock.save).toHaveBeenCalledWith({ ...fresh.values, execution: { ...fresh.values.execution, max_tool_loops: 29 } }, 1)
  })

  it('cancels, restores draft defaults, and saves only the requested group with its version', async () => {
    const data = fixture()
    data.version = 4
    data.values.execution.max_tool_loops = 39
    mock.save.mockImplementation(async values => ({ ...data, values, version: 5 }))
    host(<PreferenceSection group="execution" />, data)
    const input = screen.getByRole('spinbutton', { name: '每轮工具循环上限' })
    fireEvent.change(input, { target: { value: '50' } })
    fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
    expect(input).toHaveValue(39)
    expect(mock.save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '恢复初始值' }))
    expect(input).toHaveValue(20)
    expect(mock.save).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByRole('status')
    expect(mock.save).toHaveBeenCalledWith({ ...data.values, execution: data.defaults.execution }, 4)
    expect(screen.getByRole('status')).toHaveTextContent('下一次新任务生效')
  })

  it('keeps a failed draft and rejects overwriting a newer version until explicitly reloaded', async () => {
    mock.save.mockRejectedValue(new Error('设置版本已更新'))
    const { client } = host(<PreferenceSection group="retrieval" />)
    fireEvent.change(screen.getByRole('spinbutton', { name: '扫描文件上限' }), { target: { value: '17' } })
    await act(async () => { client.setQueryData(preferencesKey, { ...fixture(), version: 9 }) })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await screen.findByRole('alert')
    expect(mock.save.mock.calls[0][1]).toBe(0)
    expect(screen.getByRole('spinbutton', { name: '扫描文件上限' })).toHaveValue(17)
    mock.get.mockResolvedValue({ ...fixture(), version: 9 })
    fireEvent.click(screen.getByRole('button', { name: '重新读取并放弃草稿' }))
    await waitFor(() => expect(screen.getByRole('spinbutton', { name: '扫描文件上限' })).toHaveValue(2000))
  })

  it('shows advanced values as read-only and disables writes when no settings store exists', () => {
    const first = host(<PreferenceSection group="advanced" />)
    expect(screen.getByText('压缩触发比例')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '保存' })).not.toBeInTheDocument()
    first.unmount()
    host(<PreferenceSection group="memory" />, { ...fixture(), writable: false })
    expect(screen.getByRole('checkbox', { name: '启用长期记忆' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '恢复初始值' })).toBeDisabled()
  })

  it('applies saved appearance immediately and follows system theme and motion changes', async () => {
    const listeners = new Map<string, () => void>()
    const dark = { matches: true, addEventListener: (_: string, fn: () => void) => listeners.set('dark', fn), removeEventListener: vi.fn() }
    const reduced = { ...dark, matches: false, addEventListener: (_: string, fn: () => void) => listeners.set('reduced', fn) }
    vi.stubGlobal('matchMedia', (query: string) => query.includes('color-scheme') ? dark : reduced)
    const data = fixture()
    data.values.general = { ...generalDefaults, theme: 'system', font_size: 'large' }
    const { client, unmount } = host(<PreferencesProvider><span>页面</span></PreferencesProvider>, data)
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    expect(document.documentElement).toHaveAttribute('data-font-size', 'large')
    dark.matches = false
    act(() => listeners.get('dark')?.())
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    reduced.matches = true
    act(() => listeners.get('reduced')?.())
    expect(document.documentElement).toHaveAttribute('data-motion', 'reduced')
    await act(async () => client.setQueryData(preferencesKey, { ...data, values: { ...data.values, general: { ...generalDefaults, theme: 'dark', animations: false } } }))
    await waitFor(() => expect(document.documentElement).toHaveAttribute('data-theme', 'dark'))
    unmount()
    expect(document.documentElement).not.toHaveAttribute('data-theme')
  })

  it('uses the saved Think default but preserves an explicit per-message collapse', () => {
    const { rerender } = render(<GeneralContext value={{ ...generalDefaults, reasoning_expanded: true }}><ReasoningRow text="真实思考" running={false} /></GeneralContext>)
    expect(screen.getByRole('region', { name: '思考内容' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Think' }))
    rerender(<GeneralContext value={{ ...generalDefaults, reasoning_expanded: true }}><ReasoningRow text="新增思考" running /></GeneralContext>)
    expect(screen.queryByRole('region', { name: '思考内容' })).not.toBeInTheDocument()
  })

  it('uses Ctrl or Command + Enter and ignores Shift and IME confirmation', () => {
    const send = vi.fn()
    const scenario = emptySessionScenario
    render(<GeneralContext value={{ ...generalDefaults, send_shortcut: 'ctrl_enter' }}>
      <Composer session={scenario.session} plan={scenario.plan} meter={scenario.context} takeover={scenario.takeover} modelMenu={null}
        actions={{ draft: '测试', onDraftChange: vi.fn(), onSend: send, canSend: true, onToggleModelMenu: vi.fn(), onToggleMeter: vi.fn() }} />
    </GeneralContext>)
    const input = screen.getByRole('textbox', { name: '给智能体发消息' })
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true, shiftKey: true })
    fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true, isComposing: true })
    fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true, keyCode: 229 })
    expect(send).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true })
    fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
    expect(send).toHaveBeenCalledTimes(2)
  })
})
