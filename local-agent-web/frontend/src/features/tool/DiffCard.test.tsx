import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { diffScenario } from '../../fixtures/scenarios'
import type { FileDiffView } from '../../fixtures/types'
import { DiffCard } from './DiffCard'

function diffUnderTest(): FileDiffView {
  for (const message of diffScenario.messages) {
    if (message.kind === 'tool' && message.tool.card.kind === 'file-mutation') return message.tool.card.diff
  }
  throw new Error('diff fixture is missing')
}

describe('DiffCard', () => {
  const diff = diffUnderTest()

  it('is the double-hunk prototype with additions and deletions', () => {
    expect(diff.hunks).toHaveLength(2)
    expect(diff.additions).toBeGreaterThan(0)
    expect(diff.deletions).toBeGreaterThan(0)
  })

  it('derives every hunk header from its own body', () => {
    for (const hunk of diff.hunks) {
      const oldCount = hunk.lines.filter((line) => line.kind !== 'add').length
      const newCount = hunk.lines.filter((line) => line.kind !== 'del').length
      expect(hunk.header).toContain(`@@ -${String(hunk.lines[0]?.oldLine ?? 0)},${String(oldCount)}`)
      expect(hunk.header).toContain(`+${String(hunk.lines[0]?.newLine ?? 0)},${String(newCount)}`)
    }
  })

  it('numbers added and deleted lines on one side only', () => {
    for (const hunk of diff.hunks) {
      for (const line of hunk.lines) {
        if (line.kind === 'add') {
          expect(line.oldLine).toBeNull()
          expect(line.newLine).not.toBeNull()
        } else if (line.kind === 'del') {
          expect(line.oldLine).not.toBeNull()
          expect(line.newLine).toBeNull()
        } else {
          expect(line.oldLine).not.toBeNull()
          expect(line.newLine).not.toBeNull()
        }
      }
    }
  })

  it('renders the path, the counts and one sign per changed line', () => {
    const { container } = render(<DiffCard diff={diff} />)
    expect(screen.getByText(diff.path)).toBeInTheDocument()
    expect(screen.getByText(`+${String(diff.additions)}`)).toBeInTheDocument()
    expect(screen.getByText(`−${String(diff.deletions)}`)).toBeInTheDocument()

    const signs = [...container.querySelectorAll('span')].map((span) => span.textContent)
    expect(signs.filter((text) => text === '+')).toHaveLength(diff.additions)
    expect(signs.filter((text) => text === '−')).toHaveLength(diff.deletions)
  })

  it('renders the long line in full so the body scrolls instead of truncating', () => {
    render(<DiffCard diff={diff} />)
    expect(screen.getByText(/compact_long_tool_outputs_after_tokens: int = Field/)).toBeInTheDocument()
  })

  it('does not wrap the diff in a raw JSON or text dump', () => {
    const { container } = render(<DiffCard diff={diff} />)
    expect(container.querySelectorAll('pre')).toHaveLength(0)
  })
})
