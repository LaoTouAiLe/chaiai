import type { ReactElement } from 'react'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ErrorBoundary } from './ErrorBoundary'

function Boom(): ReactElement {
  throw new Error('渲染炸了')
}

/** React 捕获渲染错误时会打 console.error；测试里静音，别把噪音当断言。 */
function quietConsole() {
  return vi.spyOn(console, 'error').mockImplementation(() => {})
}

describe('ErrorBoundary', () => {
  it('shows a screen the user can act on instead of a blank page', () => {
    const spy = quietConsole()
    try {
      render(<ErrorBoundary><Boom /></ErrorBoundary>)

      expect(screen.getByRole('alert')).toBeInTheDocument()
      expect(screen.getByText('页面出错了')).toBeInTheDocument()
      // 错误原文要露出来：只说"出错了"，用户与维护者都无从下手
      expect(screen.getByText(/渲染炸了/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: '重新加载' })).toBeInTheDocument()
    } finally {
      spy.mockRestore()
    }
  })

  it('copies the error text so it can be sent to the maintainer', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const spy = quietConsole()
    try {
      render(<ErrorBoundary><Boom /></ErrorBoundary>)

      fireEvent.click(screen.getByRole('button', { name: '复制错误信息' }))

      await waitFor(() => expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument())
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining('渲染炸了'))
    } finally {
      spy.mockRestore()
    }
  })
})
