import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { App } from './App'

function openScenario(id: string): void {
  window.history.replaceState({}, '', `/?state=${id}`)
}

/**
 * The sidebar row whose title matches.
 *
 * A title can also appear in the session header, so the sidebar row is the match
 * with an `<li>` ancestor.
 */
function sessionRow(title: string): HTMLElement {
  for (const node of screen.getAllByText(title)) {
    const row = node.closest('li')
    if (row !== null) return row
  }
  throw new Error(`no session row for ${title}`)
}

describe('App sidebar state', () => {
  it('takes the selected session run state from the scenario, not the fixture', () => {
    openScenario('approval-expired')
    render(<App />)

    // The fixture row is "awaiting-approval", but this scenario's session is idle:
    // nothing is pending, so no row may claim a decision is waiting.
    const row = sessionRow('清理构建产物')
    expect(within(row).getByText('空闲')).toBeInTheDocument()
    expect(screen.queryByText('等待审批')).not.toBeInTheDocument()
  })

  it('still shows a pending decision while one really is pending', () => {
    openScenario('approval')
    render(<App />)
    expect(within(sessionRow('清理构建产物')).getByText('等待审批')).toBeInTheDocument()
  })

  it('leaves unselected rows on their own fixture state', () => {
    openScenario('shell')
    render(<App />)
    // The selected row is the shell session; the approval row is untouched.
    expect(within(sessionRow('给 README 补一段快速开始')).getByText('已完成')).toBeInTheDocument()
    expect(within(sessionRow('清理构建产物')).getByText('等待审批')).toBeInTheDocument()
  })

  it('highlights nothing and explains the disabled input when no workspace is selected', () => {
    openScenario('empty-no-workspace')
    render(<App />)
    const current = screen
      .getAllByRole('listitem')
      .filter((row) => row.getAttribute('aria-current') === 'true')
    expect(current).toHaveLength(0)
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeDisabled()
    expect(screen.getByText(/未选择工作区：发送已禁用/)).toBeInTheDocument()
  })

  it('marks exactly the scenario session as current', () => {
    openScenario('tools')
    render(<App />)
    const rows = screen.getAllByRole('listitem')
    const current = rows.filter((row) => row.getAttribute('aria-current') === 'true')
    expect(current).toHaveLength(1)
    expect(within(current[0] as HTMLElement).getByText(/^定位构建失败原因/)).toBeInTheDocument()
  })
})
