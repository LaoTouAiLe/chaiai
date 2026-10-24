import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { errorCodesScenario, errorsScenario } from '../../fixtures/scenarios'
import type { ErrorNoticeView } from '../../fixtures/types'
import { ErrorNotice } from './ErrorNotice'

function notices(): readonly ErrorNoticeView[] {
  return errorsScenario.messages.flatMap((message) => (message.kind === 'error' ? [message.notice] : []))
}

/**
 * 打开一条的详情（2026-10-16 起**默认收起成一行**，见组件注释）。
 *
 * 点标题即可展开：标题在一个 `button` 里，点它冒泡到按钮上。
 */
function expand(title: string) {
  // 按**可访问名**点那个展开按钮（点 `<span>` 不够稳：那是在依赖事件冒泡的细节）。
  fireEvent.click(screen.getByRole('button', { name: title }))
}

describe('ErrorNotice', () => {
  it('starts as a single line, closes and stays closed when reopened', () => {
    const notice = notices()[0]!
    const view = render(<ErrorNotice notice={notice} />)

    // **默认一行**：只有标题，原因/下一步/重试口径都不铺在对话里。
    expect(screen.getByText(notice.title)).toBeInTheDocument()
    expect(screen.queryByText(notice.message)).not.toBeInTheDocument()

    expand(notice.title)
    expect(screen.getByRole('button', { name: notice.title })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
    expect(screen.getByText(notice.message)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: `关闭错误提示：${notice.title}` }))
    expect(screen.queryByText(notice.message)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: `查看已关闭的错误：${notice.title}` }))
    expect(screen.getByText(notice.title)).toBeInTheDocument()
    // 重开后**仍是展开态**：组件没卸载，展开态是界面状态（用户刚点开过，重开时给他同样的视图）。
    // 这条如实钉住现行为，不写"应该怎样"的假断言。
    expect(screen.getByText(notice.message)).toBeInTheDocument()

    // 换一条新的诊断 id：它不是"已被关闭"的那条，所以会重新显示（按当前展开态渲染）。
    // 这里**不再点一次展开**——面板本来就是展开的，多点一次等于又收起来（这一条踩过一次）。
    view.rerender(<ErrorNotice notice={{ ...notice, diagnosticId: 'new-error' }} />)
    expect(screen.getByText(notice.message)).toBeInTheDocument()
  })
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
      expand(notice.title)
      expect(screen.getByText(notice.hint)).toBeInTheDocument()
      unmount()
    }
  })

  it('gives retry guidance without a dead resend button', () => {
    const [authFailed, rateLimited, retryExhausted] = notices()
    expect(authFailed).toBeDefined()
    expect(rateLimited).toBeDefined()
    expect(retryExhausted).toBeDefined()

    const auth = authFailed as ErrorNoticeView
    const { unmount: unmountAuth } = render(<ErrorNotice notice={auth} />)
    expect(screen.queryByRole('button', { name: '重新发送' })).not.toBeInTheDocument()
    // The reason is per-notice; a shared "先处理配置" would be wrong for the
    // server-side codes below.
    expand(auth.title)
    expect(screen.getByText(auth.blockedReason ?? '')).toBeInTheDocument()
    unmountAuth()

    const rate = rateLimited as ErrorNoticeView
    const { unmount: unmountRate } = render(<ErrorNotice notice={rate} />)
    expand(rate.title)
    expect(screen.getByText('检查后可在输入框重新发送')).toBeInTheDocument()
    unmountRate()

    const retry = retryExhausted as ErrorNoticeView
    render(<ErrorNotice notice={retry} />)
    expand(retry.title)
    expect(screen.getByText('检查后可在输入框重新发送')).toBeInTheDocument()
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
      expand(notice.title)
      expect(screen.getByText(notice.blockedReason ?? ''), notice.code).toBeInTheDocument()
      expect(screen.getByText(notice.hint), notice.code).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '重新发送' })).not.toBeInTheDocument()
      unmount()
    }
  })

  it('shows a stable code and a diagnostic id without leaking secrets or paths', () => {
    const notice = notices()[0] as ErrorNoticeView
    const { container } = render(<ErrorNotice notice={notice} />)
    expand(notice.title)

    const text = container.textContent ?? ''
    expect(text).toContain('MODEL_AUTH_FAILED')
    expect(text).toContain('err-4f3a91')

    // Never a key, an absolute private path or a raw provider payload.
    expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
    expect(text).not.toMatch(/[A-Za-z]:\\/)
    expect(text).not.toMatch(/"(error|status|code)"\s*:/)
  })
})
