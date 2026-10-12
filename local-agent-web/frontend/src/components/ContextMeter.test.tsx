import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ContextMeter, MeterPanel } from './ContextMeter'

/** The compact readout. The detail block repeats "未知" by design. */
function readout(container: HTMLElement): string {
  return container.querySelector('.readout')?.textContent ?? ''
}

describe('ContextMeter', () => {
  it('shows the used and window counts for a known request', () => {
    render(<ContextMeter meter={{ usedTokens: 371_000, windowTokens: 1_000_000, scope: 'previous' }} />)
    expect(screen.getByText('371k / 1000k')).toBeInTheDocument()
    expect(screen.getByRole('img')).toHaveAccessibleName(/上下文已用 37%（371k \/ 1000k，上一请求）/)
  })

  it('reports the exact counts on hover', () => {
    const { container } = render(
      <ContextMeter meter={{ usedTokens: 371_000, windowTokens: 1_000_000, scope: 'previous' }} />,
    )
    expect(container.firstElementChild).toHaveAttribute(
      'title',
      '上下文已用 37%（371,000 / 1,000,000 tokens，上一请求）',
    )
  })

  it('reports over-budget usage as more than 100% while the ring fill stays capped', () => {
    // 分子超过分母（发送前估算超出窗口）时，圆环填充封顶整圈，但**数字不能谎称 100%**：
    // 过去两者一起被 Math.min 钳掉，于是标题写"100%"、读数却是 2000k / 1000k，自相矛盾。
    const { container } = render(
      <ContextMeter meter={{ usedTokens: 2_000_000, windowTokens: 1_000_000, scope: 'current' }} />,
    )

    expect(screen.getByRole('img')).toHaveAccessibleName(/上下文已用 200%（2000k \/ 1000k，当前请求）/)
    expect(container.firstElementChild).toHaveAttribute('title', expect.stringContaining('上下文已用 200%'))
    expect(container.querySelector('[role="img"]')?.getAttribute('style')).toContain('--law-meter-ratio: 1')
  })

  it('keeps counts under one thousand unrounded', () => {
    render(<ContextMeter meter={{ usedTokens: 842, windowTokens: 1_000_000, scope: 'current' }} />)
    expect(screen.getByText('842 / 1000k')).toBeInTheDocument()
  })

  it('marks the in-flight request differently from the previous one', () => {
    render(<ContextMeter meter={{ usedTokens: 84_000, windowTokens: 128_000, scope: 'current' }} />)
    expect(screen.getByRole('img')).toHaveAccessibleName(/当前请求/)
  })

  it('reports unknown instead of inventing zero', () => {
    const { container } = render(
      <ContextMeter meter={{ usedTokens: null, windowTokens: null, scope: 'unknown' }} />,
    )
    expect(readout(container)).toBe('未知')
    expect(screen.getByRole('img')).toHaveAccessibleName(/上下文用量未知（尚未组装）/)
    expect(readout(container)).not.toContain('%')
  })

  it('keeps the window when only the usage is unknown', () => {
    const { container } = render(
      <ContextMeter meter={{ usedTokens: null, windowTokens: 128_000, scope: 'unknown' }} />,
    )
    expect(readout(container)).toBe('未知')
  })

  it('explains the 口径 on hover and suppresses the popover while the panel is open', () => {
    const meter = { usedTokens: 84_000, windowTokens: 128_000, scope: 'current' } as const
    const { unmount } = render(<ContextMeter meter={meter} />)
    const tooltip = screen.getByRole('tooltip')
    expect(within(tooltip).getByText('84,000 / 128,000 tokens')).toBeInTheDocument()
    expect(within(tooltip).getByText('66%')).toBeInTheDocument()
    expect(within(tooltip).getByText(/当前请求 · 正在运行的那一轮请求/)).toBeInTheDocument()
    expect(within(tooltip).getByText(/窗口必须由配置或可信的能力检测确认/)).toBeInTheDocument()
    unmount()

    // With the panel open the same content is pinned, so the popover is gone.
    render(<ContextMeter meter={meter} open onToggle={vi.fn()} />)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '收起上下文详情' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('forwards a ring click instead of doing nothing', async () => {
    // 圆环一直自称"展开上下文详情"且带 aria-expanded，但曾经没有 onClick：
    // 悬停能看、键盘用户点不动（matrix 22/23 要求没有指针也能拿到同一份详情）。
    const user = userEvent.setup()
    const onToggle = vi.fn()
    render(
      <ContextMeter meter={{ usedTokens: 84_000, windowTokens: 128_000, scope: 'current' }} onToggle={onToggle} />,
    )

    await user.click(screen.getByRole('button', { name: '展开上下文详情' }))

    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('does not claim to be expandable when nobody wired it', () => {
    // 惰性按钮不谎称展开态：没有 handler 时连 aria-expanded 都不渲染。
    render(<ContextMeter meter={{ usedTokens: 84_000, windowTokens: 128_000, scope: 'current' }} />)

    expect(screen.getByRole('button', { name: '展开上下文详情' })).not.toHaveAttribute('aria-expanded')
  })

  it('lets the pinned panel be closed, not just opened', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<MeterPanel meter={{ usedTokens: null, windowTokens: null, scope: 'unknown' }} onClose={onClose} />)

    await user.click(screen.getByRole('button', { name: '收起上下文详情' }))

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('keeps the same detail reachable without a pointer', () => {
    render(<MeterPanel meter={{ usedTokens: null, windowTokens: null, scope: 'unknown' }} />)
    const panel = screen.getByRole('region', { name: '上下文详情' })
    expect(within(panel).getByText(/还没有组装出可计量的请求/)).toBeInTheDocument()
    expect(within(panel).getByText(/窗口未知时不显示比例/)).toBeInTheDocument()
    expect(within(panel).getByText(/旧轮次仍显示当时的窗口/)).toBeInTheDocument()
  })
})
