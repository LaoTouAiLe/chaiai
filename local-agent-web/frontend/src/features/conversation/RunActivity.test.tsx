import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunActivityView } from '../../fixtures/types'
import { RunActivity } from './RunActivity'

const activity: RunActivityView = {
  id: 'run-test', active: true, label: '正在处理请求', detail: '可以继续写草稿。',
  startedAt: '2026-10-02T10:00:00Z',
}
afterEach(() => { vi.useRealTimers() })

describe('RunActivity', () => {
  it('uses the persisted submission time after a reload and cleans up its clock', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-02T10:01:02Z'))
    const { unmount } = render(<RunActivity activity={activity} />)
    expect(screen.getByText('本轮已用 1 分 2 秒')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(2_000))
    expect(screen.getByText('本轮已用 1 分 4 秒')).toBeInTheDocument()
    // Clock ticks are visual only and must not spam the live announcement.
    expect(screen.getByRole('status')).toHaveTextContent('正在处理请求')
    expect(screen.getByText('本轮已用 1 分 4 秒')).toHaveAttribute('aria-hidden', 'true')
    unmount()
    expect(vi.getTimerCount()).toBe(0)
  })

  it.each([null, 'not-a-date', '1970-01-01T00:00:00Z'])('does not invent elapsed time for %s', (startedAt) => {
    render(<RunActivity activity={{ ...activity, startedAt }} />)
    expect(screen.queryByText(/本轮已用/)).not.toBeInTheDocument()
  })

  it('stops animating and timing while an approval needs a decision', () => {
    vi.useFakeTimers()
    render(<RunActivity activity={{ ...activity, active: false, label: '等待你确认' }} />)
    expect(screen.getByRole('region', { name: '当前运行状态' })).toHaveAttribute('data-active', 'false')
    expect(screen.getByRole('status')).toHaveTextContent('等待你确认')
    expect(screen.queryByText(/本轮已用/)).not.toBeInTheDocument()
    expect(vi.getTimerCount()).toBe(0)
  })
})
