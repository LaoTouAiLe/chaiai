import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { errorCodesScenario, errorsScenario } from '../../fixtures/scenarios'
import type { ErrorNoticeView } from '../../fixtures/types'
import { ErrorNotice } from './ErrorNotice'

function notices(): readonly ErrorNoticeView[] {
  return errorsScenario.messages.flatMap((message) => (message.kind === 'error' ? [message.notice] : []))
}

describe('ErrorNotice', () => {
  it('covers the three provider failure prototypes', () => {
    expect(notices().map((notice) => notice.code)).toEqual([
      'MODEL_AUTH_FAILED',
      'MODEL_RATE_LIMITED',
      'MODEL_RETRY_EXHAUSTED',
    ])
  })

  it('always states what the user can do next', () => {
    for (const notice of notices()) {
      expect(notice.hint.length).toBeGreaterThan(10)
      const { unmount } = render(<ErrorNotice notice={notice} />)
      expect(screen.getByText(notice.hint)).toBeInTheDocument()
      unmount()
    }
  })

  it('offers retry only when the failure is retryable', () => {
    const [authFailed, rateLimited, retryExhausted] = notices()
    expect(authFailed).toBeDefined()
    expect(rateLimited).toBeDefined()
    expect(retryExhausted).toBeDefined()

    const { unmount: unmountAuth } = render(<ErrorNotice notice={authFailed as ErrorNoticeView} />)
    expect(screen.queryByRole('button', { name: '重新发送' })).not.toBeInTheDocument()
    // The reason is per-notice; a shared "先处理配置" would be wrong for the
    // server-side codes below.
    expect(screen.getByText(authFailed?.blockedReason ?? '')).toBeInTheDocument()
    unmountAuth()

    const { unmount: unmountRate } = render(<ErrorNotice notice={rateLimited as ErrorNoticeView} />)
    expect(screen.getByRole('button', { name: '重新发送' })).toBeInTheDocument()
    unmountRate()

    render(<ErrorNotice notice={retryExhausted as ErrorNoticeView} />)
    expect(screen.getByRole('button', { name: '重新发送' })).toBeInTheDocument()
  })

  it('gives each server-side conflict code its own next step', () => {
    const codes = errorCodesScenario.messages.flatMap((message) =>
      message.kind === 'error' ? [message.notice] : [],
    )
    expect(codes.map((notice) => notice.code)).toEqual([
      'SESSION_BUSY',
      'IDEMPOTENCY_CONFLICT',
      'RECOVERY_REQUIRED',
    ])

    // Three codes, three different sentences: they are not the same problem.
    expect(new Set(codes.map((notice) => notice.blockedReason)).size).toBe(3)
    expect(new Set(codes.map((notice) => notice.hint)).size).toBe(3)

    for (const notice of codes) {
      expect(notice.retryable, notice.code).toBe(false)
      expect(notice.blockedReason ?? '', notice.code).not.toMatch(/需先处理配置/)
      const { unmount } = render(<ErrorNotice notice={notice} />)
      expect(screen.getByText(notice.blockedReason ?? ''), notice.code).toBeInTheDocument()
      expect(screen.getByText(notice.hint), notice.code).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '重新发送' })).not.toBeInTheDocument()
      unmount()
    }
  })

  it('shows a stable code and a diagnostic id without leaking secrets or paths', () => {
    const { container } = render(<ErrorNotice notice={notices()[0] as ErrorNoticeView} />)
    const text = container.textContent ?? ''
    expect(text).toContain('MODEL_AUTH_FAILED')
    expect(text).toContain('err-4f3a91')

    // Never a key, an absolute private path or a raw provider payload.
    expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
    expect(text).not.toMatch(/[A-Za-z]:\\/)
    expect(text).not.toMatch(/"(error|status|code)"\s*:/)
  })
})
