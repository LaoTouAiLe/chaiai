import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { MessageView } from '../../fixtures/types'
import { WebSources } from './WebSources'

const tool = (id: string, kind: 'search' | 'read', url = 'https://docs.example.com/mcp'): MessageView => ({
  kind: 'tool', id, tool: {
    name: kind === 'search' ? 'web_search' : 'web_fetch', status: 'succeeded', summary: '',
    durationLabel: '', target: null, expanded: false, card: { kind: 'generic', headline: '', detail: '' },
    webSources: [{ title: 'MCP 官方文档', url, kind, retrievedAt: '2026-10-14T12:00:00Z' }],
  },
})

describe('WebSources', () => {
  it('deduplicates search and fetch and preserves the fact that the body was read', () => {
    const rows = [tool('search', 'search'), tool('fetch', 'read')]
    render(<WebSources rows={rows} answer="参考 [MCP](https://docs.example.com/mcp)。" />)
    expect(screen.getByRole('link', { name: 'MCP 官方文档' })).toHaveAttribute('href', 'https://docs.example.com/mcp')
    expect(screen.getByText('回答引用 · 已读取')).toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(screen.getByText(/获取于/)).toHaveAttribute('datetime', '2026-10-14T12:00:00Z')
  })

  it('never claims that search results have been read or that every hit was cited', () => {
    render(<WebSources rows={[tool('s', 'search')]} answer="没有引用。https://docs.example.com/mcp-fake" />)
    expect(screen.getByText('搜索结果')).toBeInTheDocument()
    expect(screen.queryByText(/回答引用/)).not.toBeInTheDocument()
  })

  it('drops failed results, non-http links and links invented in the answer', () => {
    const failed = tool('failed', 'read')
    if (failed.kind === 'tool') failed.tool.status = 'failed'
    const rows = [failed, tool('unsafe', 'read', 'javascript:alert(1)')]
    const { container } = render(<WebSources rows={rows} answer="https://invented.example.com/" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('uses server citation state instead of treating a matching URL as verified', () => {
    const row = tool('read', 'read')
    if (row.kind === 'tool') row.tool.webSources = [{
      title: '真实来源', url: 'https://docs.example.com/mcp', kind: 'read', cited: false,
      sourceId: 'web-1234567890123456', provider: 'tavily', publishedAt: '2026-10-14',
    }]
    render(<WebSources rows={[row]} answer="https://docs.example.com/mcp" />)
    expect(screen.queryByText(/回答引用/)).not.toBeInTheDocument()
    expect(screen.getByText('检索：tavily')).toBeInTheDocument()
    expect(screen.getByText('发布时间：2026-10-14')).toBeInTheDocument()
  })
})
