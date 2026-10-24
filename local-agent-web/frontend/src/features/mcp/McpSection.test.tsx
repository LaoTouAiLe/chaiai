// 「MCP 外部工具」分区的取数与写入（2026-10-20；契约 §6.13）。
//
// 只钉四件事：规模行把"多少直接可见、多少按需检索"说清楚；工具名与档位真的渲染出来；
// 配置写坏时把服务端的原话摆出来；开关只发**动过的那一项**。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({
  getMcp: vi.fn(),
  setMcpServer: vi.fn(),
  reloadMcp: vi.fn(),
  addMcpServer: vi.fn(),
  editMcpServer: vi.fn(),
  deleteMcpServer: vi.fn(),
  setCapabilities: vi.fn(),
}))

vi.mock('../../api/client', async importOriginal => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getMcp: mock.getMcp,
  setMcpServer: mock.setMcpServer,
  reloadMcp: mock.reloadMcp,
  addMcpServer: mock.addMcpServer,
  editMcpServer: mock.editMcpServer,
  deleteMcpServer: mock.deleteMcpServer,
  setCapabilities: mock.setCapabilities,
}))

import { McpSection } from './McpSection'

const VIEW = {
  enabled: true,
  config_path: '/data/chaiai/mcp.json',
  config_error: null,
  store_unavailable: false,
  servers: [
    {
      name: 'lanhu',
      command: 'npx',
      args: ['-y', 'mcp-lanhu'],
      env_keys: ['TOKEN'],
      enabled: true,
      status: 'connected' as const,
      tool_count: 2,
      tools: [
        {
          name: 'mcp__lanhu__lanhu_design',
          tool: 'lanhu_design',
          description: '读取设计稿',
          exposure: 'direct' as const,
          reason: null,
        },
        {
          name: 'mcp__lanhu__lanhu_page',
          tool: 'lanhu_page',
          description: '读取页面标注',
          exposure: 'deferred' as const,
          reason: 'over_direct_quota',
        },
      ],
      failure_kind: null,
      failure_message: null,
    },
  ],
  summary: {
    configured: 1,
    enabled: 1,
    connected: 1,
    failed: 0,
    tools: 2,
    direct: 1,
    deferred: 1,
  },
}

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {children}
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mock.getMcp.mockReset()
  mock.setMcpServer.mockReset()
  mock.reloadMcp.mockReset()
  mock.addMcpServer.mockReset()
  mock.setMcpServer.mockResolvedValue(VIEW)
  mock.reloadMcp.mockResolvedValue(VIEW)
  mock.addMcpServer.mockResolvedValue(VIEW)
  mock.editMcpServer.mockReset().mockResolvedValue(VIEW)
  mock.deleteMcpServer.mockReset().mockResolvedValue({ ...VIEW, servers: [] })
  mock.setCapabilities.mockReset().mockResolvedValue({ switches: { mcp: false } })
})

describe('McpSection', () => {
  it('states the scale facts and renders the tool names with their exposure', async () => {
    mock.getMcp.mockResolvedValue(VIEW)

    render(<McpSection />, { wrapper })

    expect(await screen.findByText('lanhu')).toBeInTheDocument()
    expect(screen.getByText(/已配置 1/)).toBeInTheDocument()
    expect(screen.getByText(/其余 1 按需检索/)).toBeInTheDocument()
    expect(screen.getByText('已连接')).toBeInTheDocument()
    // 工具名必须可见：用户要能核对自己配的 server 暴露了什么。
    expect(screen.getByText('mcp__lanhu__lanhu_design')).toBeInTheDocument()
    expect(screen.getByText('直接可见')).toBeInTheDocument()
    expect(screen.getByText('按需检索')).toBeInTheDocument()
  })

  it('toggles exactly one server and sends only that field', async () => {
    mock.getMcp.mockResolvedValue(VIEW)

    render(<McpSection />, { wrapper })

    const toggle = await screen.findByRole('checkbox', { name: 'lanhu 开关' })
    fireEvent.click(toggle)

    await waitFor(() =>
      expect(mock.setMcpServer).toHaveBeenCalledWith('lanhu', { enabled: false }),
    )
  })

  it('shows the config error verbatim so the user can fix the file', async () => {
    mock.getMcp.mockResolvedValue({
      ...VIEW,
      servers: [],
      summary: { ...VIEW.summary, configured: 0, enabled: 0, connected: 0, tools: 0, direct: 0, deferred: 0 },
      config_error: '/data/chaiai/mcp.json 不是合法 JSON：第 3 行第 5 列（Expecting value）',
    })

    render(<McpSection />, { wrapper })

    expect(await screen.findByText(/第 3 行第 5 列/)).toBeInTheDocument()
  })

  it('points at the config file when nothing is configured', async () => {
    mock.getMcp.mockResolvedValue({
      enabled: false,
      config_path: '/data/chaiai/mcp.json',
      config_error: null,
      store_unavailable: false,
      servers: [],
      summary: { configured: 0, enabled: 0, connected: 0, failed: 0, tools: 0, direct: 0, deferred: 0 },
    })

    render(<McpSection />, { wrapper })

    expect(await screen.findByText(/还没有配置任何 MCP Server/)).toBeInTheDocument()
    expect(screen.getByText('/data/chaiai/mcp.json')).toBeInTheDocument()
  })

  it('reloads the file on demand', async () => {
    mock.getMcp.mockResolvedValue(VIEW)

    render(<McpSection />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: '重读配置' }))

    await waitFor(() => expect(mock.reloadMcp).toHaveBeenCalled())
  })

  it('disables one tool by sending only disabled_tools', async () => {
    mock.getMcp.mockResolvedValue(VIEW)

    render(<McpSection />, { wrapper })

    // 点工具 chip = 停用它。只记"禁用了哪些"：新工具默认可用（与技能/能力开关同一口径）。
    fireEvent.click(await screen.findByRole('button', { name: 'mcp__lanhu__lanhu_page 启用' }))

    await waitFor(() =>
      expect(mock.setMcpServer).toHaveBeenCalledWith('lanhu', {
        disabled_tools: ['lanhu_page'],
      }),
    )
  })

  it('re-enables a disabled tool by removing it from the list', async () => {
    mock.getMcp.mockResolvedValue({
      ...VIEW,
      servers: [
        {
          ...VIEW.servers[0],
          tools: [
            { ...VIEW.servers[0].tools[0], disabled: true },
            VIEW.servers[0].tools[1],
          ],
        },
      ],
    })

    render(<McpSection />, { wrapper })

    fireEvent.click(
      await screen.findByRole('button', { name: 'mcp__lanhu__lanhu_design 启用' }),
    )

    await waitFor(() =>
      expect(mock.setMcpServer).toHaveBeenCalledWith('lanhu', { disabled_tools: [] }),
    )
  })

  it('adds a server preserving arguments with spaces and env values with equals', async () => {
    mock.getMcp.mockResolvedValue(VIEW)

    render(<McpSection />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: '添加 Server' }))
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'demo' } })
    fireEvent.change(screen.getByLabelText('参数'), {
      target: { value: '["-y", "mcp demo"]'  },
    })
    // 值里本身带等号（cookie 就是这样）：只能按**第一个**等号切。
    fireEvent.change(screen.getByLabelText('环境变量'), {
      target: { value: 'TOKEN=a=b\nOTHER=1' },
    })
    expect(screen.getByLabelText('名称')).toHaveValue('demo')
    const save = screen.getByRole('button', { name: '保存' })
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(mock.addMcpServer).toHaveBeenCalled())
    // React Query 的 `mutationFn` 会多传一个上下文参数（client/meta/mutationKey），
    // 所以只取**第一个**参数比对——那不是我们发出去的数据。
    expect(mock.addMcpServer.mock.calls[0]?.[0]).toEqual({
      name: 'demo',
      command: 'npx',
      args: ['-y', 'mcp demo'],
      env: { TOKEN: 'a=b', OTHER: '1' },
    })
  })
})


it('edits a server while leaving credential values empty and unchanged', async () => {
  mock.getMcp.mockResolvedValue(VIEW)
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByRole('button', { name: '编辑 lanhu' }))
  expect(screen.getByLabelText('名称')).toBeDisabled()
  expect(screen.getByLabelText('命令')).toHaveValue('npx')
  expect(screen.getByLabelText('环境变量')).toHaveValue('')
  fireEvent.click(screen.getByLabelText('删除环境变量 TOKEN'))
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(mock.editMcpServer).toHaveBeenCalledWith('lanhu', {
    command: 'npx', args: ['-y', 'mcp-lanhu'], env: {}, remove_env: ['TOKEN'],
  }))
})

it('requires a separate delete confirmation and removes the card after success', async () => {
  mock.getMcp.mockResolvedValue(VIEW)
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByRole('button', { name: '删除 lanhu' }))
  expect(mock.deleteMcpServer).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '取消删除' }))
  expect(mock.deleteMcpServer).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '删除 lanhu' }))
  fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
  await waitFor(() => expect(mock.deleteMcpServer.mock.calls[0]?.[0]).toBe('lanhu'))
  await waitFor(() => expect(screen.queryByRole('button', { name: '删除 lanhu' })).not.toBeInTheDocument())
})

it('saves an empty whitelist distinctly from no whitelist', async () => {
  mock.getMcp.mockResolvedValue(VIEW)
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByRole('button', { name: 'lanhu 工具名单' }))
  fireEvent.click(screen.getByLabelText('lanhu 使用白名单'))
  fireEvent.click(screen.getByRole('button', { name: '保存工具名单' }))
  await waitFor(() => expect(mock.setMcpServer).toHaveBeenCalledWith('lanhu', { enabled_tools: [], disabled_tools: [] }))
})

it('preserves disabled tools absent from the current catalog', async () => {
  mock.getMcp.mockResolvedValue({ ...VIEW, servers: [{ ...VIEW.servers[0], disabled_tools: ['removed-tool'] }] })
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByRole('button', { name: 'mcp__lanhu__lanhu_page 启用' }))
  await waitFor(() => expect(mock.setMcpServer).toHaveBeenCalledWith('lanhu', { disabled_tools: ['removed-tool', 'lanhu_page'] }))
})

it('re-enables a tool excluded by the whitelist', async () => {
  mock.getMcp.mockResolvedValue({ ...VIEW, servers: [{ ...VIEW.servers[0], enabled_tools: [] }] })
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByRole('button', { name: 'mcp__lanhu__lanhu_page 启用' }))
  await waitFor(() => expect(mock.setMcpServer).toHaveBeenCalledWith('lanhu', { enabled_tools: ['lanhu_page'], disabled_tools: [] }))
})

it('shows the child agent boundary and disables writes when the store is unavailable', async () => {
  mock.getMcp.mockResolvedValue({ ...VIEW, store_unavailable: true })
  render(<McpSection />, { wrapper })
  expect(await screen.findByText(/子 Agent 暂不支持/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '添加 Server' })).toBeDisabled()
  expect(screen.getByLabelText('MCP 总开关')).toBeDisabled()
  expect(screen.getByLabelText('lanhu 开关')).toBeDisabled()
})

it('changes the global capability switch', async () => {
  mock.getMcp.mockResolvedValue(VIEW)
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByLabelText('MCP 总开关'))
  await waitFor(() => expect(mock.setCapabilities.mock.calls[0]?.[0]).toEqual({ mcp: false }))
})

it('rejects malformed arguments locally without sending a request', async () => {
  mock.getMcp.mockResolvedValue(VIEW)
  render(<McpSection />, { wrapper })
  fireEvent.click(await screen.findByRole('button', { name: '添加 Server' }))
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: 'bad' } })
  fireEvent.change(screen.getByLabelText('参数'), { target: { value: '{}' } })
  fireEvent.click(screen.getByRole('button', { name: '保存' }))
  expect(screen.getByRole('alert')).toHaveTextContent('JSON 字符串数组')
  expect(mock.addMcpServer).not.toHaveBeenCalled()
})
