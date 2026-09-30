import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { errorsScenario, traceScenario } from '../../fixtures/scenarios'
import { traceFromMessages } from './adapters'
import { TraceView } from './TraceView'

/** 与页面同一条数据通路：轨迹由会话消息摊平而来，测试也用同一个函数，不另造数据。 */
const trace = traceFromMessages(traceScenario.messages, traceScenario.stats?.calls ?? [])

describe('TraceView', () => {
  it('lists one row per tool call with type, parameters and result summary', () => {
    const { container } = render(<TraceView trace={trace} />)

    const rows = container.querySelectorAll('[data-trace-row]')
    expect(rows).toHaveLength(7)
    // 类型 / 参数 / 结果摘要三列都要有内容，而不是只把工具名列出来。
    expect(within(rows[0] as HTMLElement).getByText('run_shell')).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText(/command=pnpm run check/)).toBeInTheDocument()
    expect(within(rows[0] as HTMLElement).getByText(/Running 4 checks/)).toBeInTheDocument()
    // 截断过的那次结果要标出来，不能看着像完整输出。
    expect(within(rows[1] as HTMLElement).getByText('（已截断）')).toBeInTheDocument()
  })

  it('keeps selection as a view state: nothing is open until a row is chosen', () => {
    render(<TraceView trace={trace} />)
    expect(screen.queryByRole('region', { name: '轨迹检查器' })).not.toBeInTheDocument()
    expect(screen.getByText(/选择左侧任一工具调用/)).toBeInTheDocument()
  })

  it('opens the inspector on the clicked row with all five sections', () => {
    render(<TraceView trace={trace} />)
    fireEvent.click(screen.getByRole('button', { name: '查看 run_shell（失败）的检查器详情' }))

    const inspector = screen.getByRole('region', { name: '轨迹检查器' })
    for (const section of ['Summary', 'Payload', 'Result', 'Schema', 'Timing']) {
      expect(within(inspector).getByRole('heading', { name: section })).toBeInTheDocument()
    }
    // Payload 是结构化的键值对，不是把 summary 那句话再显示一遍。
    expect(within(inspector).getByText('command')).toBeInTheDocument()
    // 同一条命令既是参数也是归一化目标，因此这两处都出现；Payload 段是结构化的键值对，
    // 不是把对话行那句 summary 再显示一遍。
    expect(within(inspector).getAllByText('pnpm run check')).toHaveLength(2)
    expect(within(inspector).getByText('sha256:4d1c9a7e3b02')).toBeInTheDocument()
    // Schema 段按会话列出模型调用；带指纹的照搬（前两次调用同属一个工具集），没有的写未知。
    expect(within(inspector).getAllByText(/sha256:6f1c8a2d90be/)).toHaveLength(2)
    expect(within(inspector).getAllByText(/工具 Schema 指纹：未知/)).toHaveLength(1)
  })

  it('swaps the inspector when another row is chosen', () => {
    render(<TraceView trace={trace} />)
    fireEvent.click(screen.getByRole('button', { name: '查看 run_shell（失败）的检查器详情' }))
    expect(screen.getByText('run_shell · 失败')).toBeInTheDocument()

    // 点行本身（不是那个键盘按钮）同样要切换：鼠标路径不能被落下。
    const row = screen.getByText('vendor_scan_tool').closest('tr')
    expect(row).not.toBeNull()
    fireEvent.click(row as HTMLElement)
    expect(screen.getByText('vendor_scan_tool · 成功')).toBeInTheDocument()
  })

  it('says 未知 — never 0 — for the fields the contract does not carry', () => {
    render(<TraceView trace={trace} />)
    fireEvent.click(screen.getByRole('button', { name: '查看 vendor_scan_tool（成功）的检查器详情' }))
    const inspector = screen.getByRole('region', { name: '轨迹检查器' })

    // 目标 / 参数 / 结果摘要 / 结果指纹 / 所属轮次都没有记录：一律"未知"，不写 0。
    expect(within(inspector).getAllByText('未知').length).toBeGreaterThanOrEqual(4)
    expect(within(inspector).queryByText('0')).toBeNull()
    expect(within(inspector).getByText(/契约没有记录单条工具调用属于哪一轮/)).toBeInTheDocument()
    expect(within(inspector).getByText(/没有"哪次工具调用由哪次模型调用产生"的绑定字段/)).toBeInTheDocument()
  })

  it('shows an empty state instead of an empty table when the session has no tool calls', () => {
    render(<TraceView trace={traceFromMessages(errorsScenario.messages, [])} />)
    expect(screen.getByText(/这个会话还没有工具调用记录/)).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})