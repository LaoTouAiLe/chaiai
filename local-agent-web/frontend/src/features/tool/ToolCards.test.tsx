import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { toolsScenario } from '../../fixtures/scenarios'
import type { ToolCardView } from '../../fixtures/types'
import { ToolCard } from './ToolCards'
import { ToolRow } from './ToolRow'

function cards(): readonly ToolCardView[] {
  return toolsScenario.messages.flatMap((message) => (message.kind === 'tool' ? [message.tool.card] : []))
}

function cardOf<K extends ToolCardView['kind']>(kind: K): Extract<ToolCardView, { kind: K }> {
  for (const card of cards()) {
    if (card.kind === kind) return card as Extract<ToolCardView, { kind: K }>
  }
  throw new Error(`missing ${kind} card in the tools fixture`)
}

describe('ToolCard', () => {
  it('covers every card shape the conversation must render', () => {
    expect(cards().map((card) => card.kind)).toEqual([
      'terminal',
      'search',
      'search',
      'read',
      'todo',
      'generic',
      'terminal',
    ])
  })

  it('covers both a failed and a successful command', () => {
    const exits = cards().flatMap((card) => (card.kind === 'terminal' ? [card.exitCode] : []))
    expect(exits).toEqual([1, 0])
  })

  it('reads a window with its own line numbers and the file total', () => {
    render(<ToolCard card={cardOf('read')} />)
    expect(screen.getByText('packages/host/webserver/src/index.ts')).toBeInTheDocument()
    expect(screen.getByText('第 41–45 行 / 共 196 行')).toBeInTheDocument()
    expect(screen.getByText('41')).toBeInTheDocument()
    expect(screen.getByText('45')).toBeInTheDocument()
  })

  it('groups matches by file and states that the list was truncated', () => {
    const card = cardOf('search')
    render(<ToolCard card={card} />)
    expect(screen.getByText('按内容匹配')).toBeInTheDocument()
    expect(screen.getByText('显示 2 个文件 / 共 42')).toBeInTheDocument()
    expect(screen.getByText('packages/client/ui-primitives/src/SearchBlock.tsx')).toBeInTheDocument()
    // A capped result must say so rather than look complete.
    expect(screen.getByText(/结果已截断，仅显示前 2 个文件/)).toBeInTheDocument()
  })

  it('renders the path shape as a flat list', () => {
    const shapes = cards().filter((card) => card.kind === 'search')
    const paths = shapes[1]
    if (paths === undefined || paths.kind !== 'search') throw new Error('missing paths search card')
    render(<ToolCard card={paths} />)
    expect(screen.getByText('按路径匹配')).toBeInTheDocument()
    expect(screen.getByText('显示 5 个路径 / 共 23')).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(5)
  })

  it('separates the terminal exit code from the output body', () => {
    render(<ToolCard card={cardOf('terminal')} />)
    expect(screen.getByText('pnpm run check')).toBeInTheDocument()
    expect(screen.getByText('退出码 1')).toBeInTheDocument()
    // The exit marker belongs to the card, not to the captured text.
    expect(screen.getByText(/1 of 4 checks failed/)).toBeInTheDocument()
    expect(screen.getByText(/输出较长，已折叠 8 行/)).toBeInTheDocument()
  })

  it('counts todo progress and labels every state', () => {
    render(<ToolCard card={cardOf('todo')} />)
    expect(screen.getByText('1/4 已完成')).toBeInTheDocument()
    // Parallel steps stay parallel: two items may be in progress at once.
    expect(screen.getAllByText('进行中')).toHaveLength(2)
    expect(screen.getAllByText('待处理')).toHaveLength(1)
  })

  it('never paints a successful exit as a failure', () => {
    const base = cardOf('terminal')

    const success = render(<ToolCard card={{ ...base, exitCode: 0, signal: null }} />)
    expect(screen.getByText('退出码 0').className).not.toMatch(/exitFailed/)
    success.unmount()

    const failure = render(<ToolCard card={{ ...base, exitCode: 2, signal: null }} />)
    expect(screen.getByText('退出码 2').className).toMatch(/exitFailed/)
    failure.unmount()

    render(<ToolCard card={{ ...base, exitCode: null, signal: 'SIGTERM' }} />)
    expect(screen.getByText('信号 SIGTERM').className).toMatch(/exitFailed/)
  })

  it('keeps unknown tools on the generic renderer', () => {
    render(<ToolCard card={cardOf('generic')} />)
    expect(screen.getByText('工具返回了结构化结果')).toBeInTheDocument()
    expect(screen.getByText('通用渲染器')).toBeInTheDocument()
  })
})

describe('ToolRow', () => {
  it('renders the specialised card for a known tool and never a generic one', () => {
    render(<ToolCard card={cardOf('read')} />)
    expect(screen.queryByText('通用渲染器')).not.toBeInTheDocument()
  })

  it('routes a file mutation to the diff card', () => {
    const message = toolsScenario.messages.find(
      (candidate) => candidate.kind === 'tool' && candidate.tool.card.kind === 'file-mutation',
    )
    // The tools fixture has no mutation; the diff scenario owns that card.
    expect(message).toBeUndefined()

    const mutation = {
      name: 'edit_file',
      summary: '精确串替换',
      status: 'succeeded' as const,
      durationLabel: '0.2s',
      target: 'notes/demo.txt',
      expanded: true,
      card: {
        kind: 'file-mutation' as const,
        diff: { path: 'notes/demo.txt', additions: 1, deletions: 0, hunks: [] },
      },
    }
    render(<ToolRow tool={mutation} />)
    // The path shows in the row and again inside the diff card it expands into.
    expect(screen.getAllByText('notes/demo.txt')).toHaveLength(2)
    expect(screen.getByRole('figure')).toBeInTheDocument()
    expect(screen.queryByText('通用渲染器')).not.toBeInTheDocument()
  })
})
