import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  emptySessionScenario,
  errorsScenario,
  loadingHistoryScenario,
  memoryHitScenario,
  scenarios,
  shellScenario,
  toolsScenario,
} from '../../fixtures/scenarios'
import { Conversation } from './Conversation'

/** Mirrors the component's display rule: `k` only above one thousand. */
function formatTokens(value: number): string {
  return value >= 1_000 ? `${(value / 1000).toFixed(1)}k tok` : `${String(value)} tok`
}

describe('Conversation', () => {
  it('keeps thought, narration and tools in chronological order, without inventing thought', () => {
    const tool = shellScenario.messages.find(message => message.kind === 'tool')!
    const messages = [
      { kind: 'user' as const, id: 'u', text: '测试', at: '' },
      { kind: 'assistant' as const, id: 'a', text: '开始查阅', at: '', reasoning: '模型思考' },
      { ...tool, id: 't1' },
      { kind: 'assistant' as const, id: 'b', text: '继续查阅', at: '' },
      { ...tool, id: 't2' },
    ]
    const { container } = render(<Conversation scenario={{ ...shellScenario, messages, stats: null }} />)
    expect(screen.getAllByRole('button', { name: 'Think' })).toHaveLength(1)
    const text = container.textContent ?? ''
    const label = '读取文件'
    expect(text.indexOf(label)).toBeGreaterThan(text.indexOf('开始查阅'))
    expect(text.lastIndexOf(label)).toBeGreaterThan(text.indexOf('继续查阅'))
  })
  it('reports unknown usage as unknown, never as zero percent', () => {
    render(<Conversation scenario={errorsScenario} />)
    expect(screen.getAllByText(/未知/).length).toBeGreaterThan(0)
    expect(screen.queryByText(/0%/)).not.toBeInTheDocument()
  })

  it('renders each message kind through its own component', () => {
    render(<Conversation scenario={shellScenario} />)
    expect(screen.getByText('在 README 里补一段最小启动步骤。')).toBeInTheDocument()
    expect(screen.getByText('读取文件')).toBeInTheDocument()
    expect(screen.getByText('修改文件')).toBeInTheDocument()
    expect(screen.queryByText(/执行过程/)).not.toBeInTheDocument()
    expect(screen.getByText('缓存命中 62%')).toBeInTheDocument()
  })

  it('sums the turn from its per-call details instead of trusting a parallel total', () => {
    const stats = toolsScenario.stats
    if (stats === null) throw new Error('tools fixture has no stats')
    render(<Conversation scenario={toolsScenario} />)

    const input = stats.calls.reduce((total, call) => total + (call.inputTokens ?? 0), 0)
    const output = stats.calls.reduce((total, call) => total + (call.outputTokens ?? 0), 0)
    expect(screen.getByText(new RegExp(`输入 ${formatTokens(input)}`))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`输出 ${formatTokens(output)}`))).toBeInTheDocument()
    // 2026-09-30：摘要把整轮耗时也写进去了，这里只钉住条数部分。
    expect(screen.getByText(new RegExp(`明细（${String(stats.calls.length)} 次模型调用`))).toBeInTheDocument()
  })

  it('labels estimated token numbers as estimates', () => {
    const { unmount } = render(<Conversation scenario={toolsScenario} />)
    expect(screen.getByText('估算')).toBeInTheDocument()
    unmount()

    render(<Conversation scenario={shellScenario} />)
    expect(screen.queryByText('估算')).not.toBeInTheDocument()
  })

  it('renders the retrieval row as its own message kind', () => {
    render(<Conversation scenario={memoryHitScenario} />)
    expect(screen.getByRole('region', { name: '检索与记忆' })).toBeInTheDocument()
    expect(screen.getByText('已命中')).toBeInTheDocument()
  })

  it('replaces the flow with the empty state instead of an empty column', () => {
    const { container } = render(<Conversation scenario={emptySessionScenario} />)
    expect(screen.getByText('开始一个新会话')).toBeInTheDocument()
    expect(screen.getByText(/会先给出计划/)).toBeInTheDocument()
    expect(container.textContent ?? '').not.toContain('缓存命中')
  })

  it('replaces the flow with the loading state and a content-free skeleton', () => {
    const { container } = render(<Conversation scenario={loadingHistoryScenario} />)
    expect(screen.getByText('正在读取会话事件…')).toBeInTheDocument()
    expect(screen.getByText(/不会重放模型调用/)).toBeInTheDocument()
    // The skeleton is shape only: six bars, no text of its own.
    expect(container.querySelectorAll('[aria-hidden="true"] > span')).toHaveLength(6)
    expect(container.textContent ?? '').not.toContain('开始一个新会话')
  })

  it('keeps every fixture internally consistent', () => {
    for (const scenario of scenarios) {
      const stats = scenario.stats
      if (stats === null || stats.calls.length === 0) continue
      const input = stats.calls.reduce((total, call) => total + (call.inputTokens ?? 0), 0)
      const output = stats.calls.reduce((total, call) => total + (call.outputTokens ?? 0), 0)
      expect(input, `${scenario.id} input total`).toBe(stats.inputTokens)
      expect(output, `${scenario.id} output total`).toBe(stats.outputTokens)
    }
  })

  it('follows live updates only while the reader remains near the bottom', () => {
    const { rerender } = render(<Conversation scenario={shellScenario} autoFollow />)
    const log = screen.getByRole('log', { name: '对话' })
    Object.defineProperty(log, 'scrollHeight', { configurable: true, value: 1000 })
    Object.defineProperty(log, 'clientHeight', { configurable: true, value: 500 })
    rerender(<Conversation scenario={{ ...shellScenario, messages: [...shellScenario.messages] }} autoFollow />)
    expect(log.scrollTop).toBe(1000)

    log.scrollTop = 100
    fireEvent.scroll(log)
    rerender(<Conversation scenario={{ ...shellScenario, messages: [...shellScenario.messages] }} autoFollow />)
    expect(log.scrollTop).toBe(100)
  })
})
