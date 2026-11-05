import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MarkdownMessage } from './MarkdownMessage'
import { FileLinkContext } from './fileLinkContext'

describe('answer code surfaces', () => {
  it('highlights source while keeping untrusted code inert', async () => {
    const { container } = render(<MarkdownMessage text={'```ts\nconst answer = "<script>bad()</script>"\n```'} />)
    await waitFor(() => expect(container.querySelector('pre')).toHaveAttribute('data-highlighted', 'true'))
    expect(container.querySelector('pre [style]')).not.toBeNull()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('pre')).toHaveTextContent('<script>bad()</script>')
  })
  it('opens a fence filename, renders marked lines, and copies a user-selected range', async () => {
    const open = vi.fn(), copy = vi.fn().mockResolvedValue(undefined)
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: copy } })
    try {
      const { container } = render(<FileLinkContext.Provider value={open}><MarkdownMessage text={'```ts title="src/app.ts" {2}\nfirst\nsecond\nthird\n```'} /></FileLinkContext.Provider>)
      fireEvent.click(screen.getByRole('button', { name: 'src/app.ts' }))
      expect(open).toHaveBeenCalledWith('src/app.ts')
      expect(container.querySelector('[data-code-line="2"]')).toHaveAttribute('data-marked')
      fireEvent.click(screen.getByRole('button', { name: '显示行号' }))
      fireEvent.click(screen.getByRole('button', { name: '标记第 1 行' }))
      fireEvent.click(screen.getByRole('button', { name: '标记第 3 行' }), { shiftKey: true })
      fireEvent.click(screen.getByRole('button', { name: '复制标记行' }))
      await waitFor(() => expect(copy).toHaveBeenCalledWith('first\nsecond\nthird'))
      expect(screen.getByRole('button', { name: '复制标记行' })).toHaveTextContent('已复制标记行')
      expect(screen.getByRole('button', { name: '复制代码' })).toHaveTextContent('复制')
      fireEvent.click(screen.getByRole('button', { name: '清除标记' }))
      expect(screen.queryByText(/已标记第/)).not.toBeInTheDocument()
    } finally {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })
  it('keeps code controls while a fence streams, and ends its running state when it closes', () => {
    const { container, rerender } = render(<MarkdownMessage text={'```unknown-lang\nfirst'} streaming />)
    fireEvent.click(screen.getByRole('button', { name: '开启代码自动换行' }))
    fireEvent.click(screen.getByRole('button', { name: '显示行号' }))
    expect(screen.getByText('代码正在生成')).toBeInTheDocument()
    rerender(<MarkdownMessage text={'```unknown-lang\nfirst\nsecond\n```\n\n回答仍在生成'} streaming />)
    expect(screen.getByRole('button', { name: '关闭代码自动换行' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '隐藏行号' })).toBeInTheDocument()
    expect(container.querySelectorAll('[data-code-line]')).toHaveLength(2)
    expect(screen.queryByText('代码正在生成')).not.toBeInTheDocument()
    expect(container.querySelector('pre')).toHaveTextContent('second')
  })
})
