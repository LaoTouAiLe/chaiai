import { describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'
import { backendErrorText } from './errorText'

function apiError(message: string, hint: string | null = null): ApiError {
  return new ApiError({ status: 400, code: 'SESSION_BUSY', message, hint })
}

describe('后端错误文案（2026-10-28）', () => {
  it('原样用后端 message：页面不再自己写一套码表', () => {
    const error = apiError('本次提交未被接受：同一会话同一时刻只允许一轮运行，请求不会排队。')
    expect(backendErrorText(error)).toBe('本次提交未被接受：同一会话同一时刻只允许一轮运行，请求不会排队。')
  })

  it('需要"下一步"时把 hint 拼在后面，不重复同一句', () => {
    const error = apiError('这个审批请求已经不在等待决策，因此没有执行任何操作。', '重新发起请求并重新确认。')
    expect(backendErrorText(error, { withHint: true })).toBe(
      '这个审批请求已经不在等待决策，因此没有执行任何操作。 重新发起请求并重新确认。',
    )
    // hint 已包含在 message 里时不重复。
    const same = apiError('重新发起请求并重新确认。', '重新发起请求并重新确认。')
    expect(backendErrorText(same, { withHint: true })).toBe('重新发起请求并重新确认。')
  })

  it('message 为空时给一句通用说法，不显示空白', () => {
    expect(backendErrorText(apiError('   '))).toBe('服务端没有接受这次操作')
  })

  it('安全兜底：文案里若混入密钥样式或 URL，退回通用说法', () => {
    expect(backendErrorText(apiError('provider said sk-live-abcdefgh'))).toBe('服务端没有接受这次操作')
    expect(backendErrorText(apiError('see https://api.deepseek.com/v1 for details'))).toBe('服务端没有接受这次操作')
  })

  it('传输层失败（不是 ApiError）说"没有响应"', () => {
    expect(backendErrorText(new Error('Failed to fetch'))).toBe('本机服务没有响应')
  })
})
