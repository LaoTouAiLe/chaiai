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
    // 轨迹与对话看的是同一份消息：夹具里现在还有一次自主委派（§18.7 的 `delegate_research`）。
    expect(rows).toHaveLength(8)
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

  it('groups by turn and reports the turn numbers, saying 未上报 where the provider said nothing', () => {
    // 2026-10-16：轨迹从"没有边界的平铺表"改成**按轮分组**，每轮头上给契约里的用量。
    // 这条同时钉住三件如实搬运：① 未知写"未知"；② **未上报 ≠ 0%**；③ 轮内逐次调用给缓存账目。
    const lastMessage = traceScenario.messages[traceScenario.messages.length - 1]!
    const grouped = traceFromMessages(traceScenario.messages, [], [
      {
        afterMessageId: lastMessage.id,
        stats: {
          turns: 1,
          steps: 0,
          cacheHitRatio: null,
          cacheReported: false,
          inputTokens: 12000,
          outputTokens: 480,
          elapsedLabel: '35.3s',
          firstTokenLabel: '1.2s',
          contextUsedTokens: 12300,
          contextWindowTokens: 1000000,
          finalState: 'blocked',
          estimated: false,
          calls: [
            {
              id: 'call-1',
              purpose: '工具决策',
              modelLabel: 'deepseek-flash',
              inputTokens: 12000,
              outputTokens: 480,
              elapsedLabel: '8.4s',
              firstTokenLabel: '1.2s',
              cacheReadTokens: null,
              reasoningTokens: 1500,
            },
          ],
        },
      },
    ])

    const { container } = render(<TraceView trace={grouped} />)

    expect(screen.getByText('第 1 轮')).toBeInTheDocument()
    // 工具行一条没少（分组只是加了一层壳）。
    expect(container.querySelectorAll('[data-trace-row]')).toHaveLength(8)
    expect(screen.getByText('12.0k')).toBeInTheDocument()
    // 供应商没上报缓存 → "未上报"，不是 0%。
    expect(screen.getByText('未上报')).toBeInTheDocument()
    expect(screen.queryByText('0%')).not.toBeInTheDocument()
    // 上下文占用按"已用 / 窗口（百分比）"给，窗口过百万写 M。
    expect(screen.getByText(/12.3k \/ 1.0M/)).toBeInTheDocument()
    // 逐次调用：缓存读没上报时逐条说清，推理 token 有值就显示。
    expect(screen.getByText(/缓存未上报/)).toBeInTheDocument()
    expect(screen.getByText(/思考 1.5k/)).toBeInTheDocument()
  })

  it('shows the approval wait, the compaction and a TTFT-split timeline', () => {
    // 2026-10-16：轨迹里此前**完全看不到**审批与压缩，也看不到"时间花在哪"。
    // 这条钉住三样：① 审批的等待时长用契约给的 `wait_ms`；② 压缩给出折叠条数；
    // ③ 时间轴把每次调用按首字切成"等首字/解码"两段（缺时刻的那一段不画，不拿别的时刻凑）。
    const lastMessage = traceScenario.messages[traceScenario.messages.length - 1]!
    const grouped = traceFromMessages(traceScenario.messages, [], [
      {
        afterMessageId: lastMessage.id,
        stats: {
          turns: 1,
          steps: 0,
          cacheHitRatio: null,
          cacheReported: false,
          inputTokens: 12000,
          outputTokens: 480,
          elapsedLabel: '35.3s',
          firstTokenLabel: '1.0s',
          estimated: false,
          calls: [
            {
              id: 'call-1',
              purpose: '工具决策',
              modelLabel: 'deepseek-flash',
              inputTokens: 12000,
              outputTokens: 480,
              elapsedLabel: '9.0s',
              firstTokenLabel: '1.0s',
              startedAt: '2026-10-16T03:00:00Z',
              firstTokenAt: '2026-10-16T03:00:01Z',
              finishedAt: '2026-10-16T03:00:09Z',
            },
          ],
        },
        events: [
          {
            kind: 'approval',
            id: 'ap-1',
            at: '2026-10-16T03:00:09Z',
            finishedAt: '2026-10-16T03:00:21Z',
            label: '用户同意删除临时文件',
            status: 'approved',
            waitMs: 12000,
            foldedMessages: null,
            tokensBefore: null,
            tokensAfter: null,
          },
          {
            kind: 'compaction',
            id: 'cmp-1',
            at: '2026-10-16T03:00:25Z',
            finishedAt: '',
            label: '上下文压缩：折叠 6 条消息',
            status: null,
            waitMs: null,
            foldedMessages: 6,
            tokensBefore: 41000,
            tokensAfter: 21000,
          },
        ],
      },
    ])

    const { container } = render(<TraceView trace={grouped} />)

    // 事件流水给出「审批」「压缩」两类标签（工具事件不进流水——它们在下面的表里）。
    expect(screen.getAllByText('审批').length).toBeGreaterThan(0)
    expect(screen.getByText('用户同意删除临时文件')).toBeInTheDocument()
    expect(screen.getByText('等待 12.0s')).toBeInTheDocument()
    expect(screen.getAllByText('压缩').length).toBeGreaterThan(0)
    expect(screen.getByText('折叠 6 条')).toBeInTheDocument()
    // 时间构成：**数字**代替那根 130 段的条形码（"首字/解码"必须出现且带时长）。
    expect(screen.getByText(/等首字 1\.0s/)).toBeInTheDocument()
    expect(screen.getByText(/解码 8\.0s/)).toBeInTheDocument()
    // 关键节点条上**只画审批与压缩**（工具与模型调用由下面的表与逐次账目交代）；
    // title 是鼠标悬停时看到的那句话。
    expect(container.querySelectorAll('[title*="等待 12.0s"]')).toHaveLength(1)
    expect(container.querySelectorAll('[title*="折叠"]')).toHaveLength(1)
  })
})
