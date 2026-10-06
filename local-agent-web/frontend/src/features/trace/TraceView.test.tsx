import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { errorsScenario, traceScenario } from '../../fixtures/scenarios'
import { traceFromMessages } from './adapters'
import { TraceView } from './TraceView'

const trace = traceFromMessages(traceScenario.messages, traceScenario.stats?.calls ?? [])

describe('TraceView', () => {
  it('shows real actions with readable names, targets and bounded results', () => {
    const { container } = render(<TraceView trace={trace} />)
    const rows = container.querySelectorAll('[data-trace-row]')
    expect(rows).toHaveLength(7)
    expect(within(rows[0] as HTMLElement).getByText('运行命令')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText('pnpm run check')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText(/Running 4 checks/)).toBeInTheDocument()
    expect(within(rows[1] as HTMLElement).getByText('（已截断）')).toBeInTheDocument()
    expect(screen.queryByText(/sha256:/)).not.toBeInTheDocument()
  })

  it('opens details with evidence and keeps parameters collapsed', () => {
    render(<TraceView trace={trace} />)
    expect(screen.queryByRole('region', { name: '轨迹检查器' })).not.toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: '查看运行命令（失败）的详情' })[0]!)

    const inspector = screen.getByRole('region', { name: '轨迹检查器' })
    expect(within(inspector).getByText('运行命令 · 失败')).toBeInTheDocument()
    expect(within(inspector).getByRole('heading', { name: '操作详情' })).toBeInTheDocument()
    expect(within(inspector).getByText('查看参数')).toBeInTheDocument()
    expect(within(inspector).getByRole('heading', { name: '时间' })).toBeInTheDocument()
    expect(within(inspector).queryByText(/Schema 指纹/)).not.toBeInTheDocument()
    expect(within(inspector).queryByText(/所属轮次/)).not.toBeInTheDocument()
  })

  it('swaps details when another row is chosen', () => {
    const { container } = render(<TraceView trace={trace} />)
    fireEvent.click(screen.getAllByRole('button', { name: '查看运行命令（失败）的详情' })[0]!)
    const unknownRow = [...container.querySelectorAll('[data-trace-row]')]
      .find((row) => row.getAttribute('data-trace-row') === 'call-d40f58')
    expect(unknownRow).toBeDefined()
    fireEvent.click(unknownRow as HTMLElement)
    expect(screen.getByText('执行操作 · 成功')).toBeInTheDocument()
  })

  it('shows an empty state for a session without actions', () => {
    render(<TraceView trace={traceFromMessages(errorsScenario.messages, [])} />)
    expect(screen.getByText('这个会话还没有操作记录。')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
