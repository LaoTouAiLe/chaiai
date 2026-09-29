import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { memoryFailedScenario, memoryHitScenario, memoryMissScenario } from '../../fixtures/scenarios'
import type { MemoryRowView } from '../../fixtures/types'
import { MemoryRow } from './MemoryRow'

function memoryOf(scenario: typeof memoryHitScenario): MemoryRowView {
  const message = scenario.messages.find((candidate) => candidate.kind === 'memory')
  if (message === undefined || message.kind !== 'memory') throw new Error('scenario has no memory row')
  return message.memory
}

describe('MemoryRow', () => {
  it('lists a hit as locators with their usage, not as content', () => {
    render(<MemoryRow memory={memoryOf(memoryHitScenario)} />)
    expect(screen.getByText('已命中')).toBeInTheDocument()
    expect(screen.getByText('3 处来源')).toBeInTheDocument()
    expect(screen.getByText('工作区文件')).toBeInTheDocument()
    expect(screen.getByText('local-agent-web/ARCHITECTURE.md:15.6')).toBeInTheDocument()
    expect(screen.getByText('长期记忆')).toBeInTheDocument()
    expect(screen.getByText('记忆 m-2412 · 写入于 09-20')).toBeInTheDocument()
    expect(screen.getByText('会话历史')).toBeInTheDocument()
    expect(screen.getByText(/不展示正文、向量或内部评分/)).toBeInTheDocument()
    expect(screen.getByText(/已写入长期记忆/)).toBeInTheDocument()
  })

  it('separates "found nothing" from "could not look"', () => {
    const { unmount } = render(<MemoryRow memory={memoryOf(memoryMissScenario)} />)
    expect(screen.getByText('未命中')).toBeInTheDocument()
    expect(screen.getByText(/不会为来源留空位，也不会编造出处/)).toBeInTheDocument()
    expect(screen.queryByText('检索失败')).not.toBeInTheDocument()
    unmount()

    render(<MemoryRow memory={memoryOf(memoryFailedScenario)} />)
    expect(screen.getByText('检索失败')).toBeInTheDocument()
    expect(screen.getByText(/本地检索索引不可用/)).toBeInTheDocument()
    expect(screen.getByText(/回答中没有引用来源/)).toBeInTheDocument()
    expect(screen.queryByText('未命中')).not.toBeInTheDocument()
  })

  it('reports a failed memory write as not affecting the answer', () => {
    render(<MemoryRow memory={memoryOf(memoryMissScenario)} />)
    expect(screen.getByText(/写入长期记忆失败/)).toBeInTheDocument()
    expect(screen.getByText(/不影响本轮回答/)).toBeInTheDocument()
  })

  it('carries locators and口径 only — no retrieved text, vectors, scores or keys', () => {
    for (const scenario of [memoryHitScenario, memoryMissScenario, memoryFailedScenario]) {
      const { container, unmount } = render(<MemoryRow memory={memoryOf(scenario)} />)
      const text = container.textContent ?? ''
      // Data-shaped leaks, not the words themselves: the hit row says in words
      // that it shows no vectors or scores.
      expect(text, scenario.id).not.toMatch(/相似度[:：]|得分[:：]|score[:：]|relevance:/i)
      expect(text, scenario.id).not.toMatch(/\b0\.\d+\b/)
      expect(text, scenario.id).not.toMatch(/\[[\s\d.,-]{12,}\]/)
      expect(text, scenario.id).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
      // A forwarded excerpt would blow this budget wide open.
      expect(text.length, scenario.id).toBeLessThan(400)
      unmount()
    }
  })
})
