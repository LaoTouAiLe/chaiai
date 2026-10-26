import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useBodyScrollLock } from './useBodyScrollLock'

describe('useBodyScrollLock（2026-10-26）', () => {
  it('locks while mounted and restores the previous value', () => {
    document.body.style.overflow = 'scroll'

    const { unmount } = renderHook(() => useBodyScrollLock())

    expect(document.body.style.overflow).toBe('hidden')
    unmount()
    expect(document.body.style.overflow).toBe('scroll')
    document.body.style.overflow = ''
  })

  it('keeps the lock until the last holder leaves', () => {
    // 这是"用计数而不是布尔"的理由：任务中心与能力中心可能先后打开，
    // 先关的那个一旦按布尔还原，另一个还开着、背后却已经开始滚了。
    const first = renderHook(() => useBodyScrollLock())
    const second = renderHook(() => useBodyScrollLock())
    expect(document.body.style.overflow).toBe('hidden')

    first.unmount()
    expect(document.body.style.overflow).toBe('hidden')

    second.unmount()
    expect(document.body.style.overflow).toBe('')
  })

  it('does nothing when inactive', () => {
    // 内嵌形态（能力面板嵌在整页里）不能锁 body：那里有祖先滚动容器，锁了整页就滚不动。
    document.body.style.overflow = ''

    const { rerender, unmount } = renderHook(
      ({ active }: { active: boolean }) => useBodyScrollLock(active),
      { initialProps: { active: false } },
    )
    expect(document.body.style.overflow).toBe('')

    rerender({ active: true })
    expect(document.body.style.overflow).toBe('hidden')

    unmount()
    expect(document.body.style.overflow).toBe('')
  })
})
