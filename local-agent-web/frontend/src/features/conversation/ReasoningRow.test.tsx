import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ReasoningRow } from './ReasoningRow'

describe('ReasoningRow', () => {
  it('defaults to folded, follows the latest line while running, and preserves expansion', () => {
    const { rerender } = render(<ReasoningRow text={'第一行\n当前思考'} running />)
    const button = screen.getByRole('button', { name: '思考中' })
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText('当前思考')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '思考内容' })).not.toBeInTheDocument()
    fireEvent.click(button)
    rerender(<ReasoningRow text={'第一行\n当前思考\n补充思考'} running={false} />)
    expect(screen.getByRole('region', { name: '思考内容' })).toHaveTextContent('补充思考')
    fireEvent.click(screen.getByRole('button', { name: '思考' }))
    expect(screen.getByText('第一行')).toBeInTheDocument()
  })

  it('renders thought as plain text and labels a truncated display', () => {
    const { container } = render(<ReasoningRow text={'<script>bad()</script>\n真实思考'} running={false} truncated />)
    fireEvent.click(screen.getByRole('button', { name: '思考' }))
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByText('思考内容展示已截断')).toBeInTheDocument()
  })
})
