import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CompressionRow } from './CompressionRow'

describe('CompressionRow', () => {
  it('states what was folded and the estimated input change', () => {
    // 2026-10-16：压缩是流程的一步（与 Think、工具行同级），要能扫出"折叠了多少、前后差多少"。
    render(
      <CompressionRow
        compression={{ running: false, foldedMessages: 151, tokensBefore: 149231, tokensAfter: 36931 }}
      />,
    )

    expect(screen.getByText('上下文压缩')).toBeInTheDocument()
    expect(screen.getByText(/折叠 151 条消息/)).toBeInTheDocument()
    // **估算**输入的前后变化（不是供应商实际量，所以不写"省了多少 token"）。
    expect(screen.getByText(/149\.2k → 36\.9k/)).toBeInTheDocument()
  })

  it('claims nothing but "running" while it is still running', () => {
    // 运行中还没有条数与前后 token：写 0 就是编数。
    render(
      <CompressionRow
        compression={{ running: true, foldedMessages: null, tokensBefore: null, tokensAfter: null }}
      />,
    )

    expect(screen.getByText('正在压缩…')).toBeInTheDocument()
    expect(screen.queryByText(/折叠/)).not.toBeInTheDocument()
    expect(screen.queryByText(/0 条/)).not.toBeInTheDocument()
  })

  it('says 未知 instead of zero when the record lacks the numbers', () => {
    render(
      <CompressionRow
        compression={{ running: false, foldedMessages: null, tokensBefore: null, tokensAfter: null }}
      />,
    )

    expect(screen.getByText(/折叠条数未知/)).toBeInTheDocument()
  })
})
