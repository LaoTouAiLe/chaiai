import { fireEvent, render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { Notice } from './Notice'

it('dismisses one notice and displays a changed scope', () => {
  const view = render(<Notice label="分支提示" scope="s1:b1">当前分支一</Notice>)
  fireEvent.click(screen.getByRole('button', { name: '关闭分支提示' }))
  view.rerender(<Notice label="分支提示" scope="s1:b1">当前分支一</Notice>)
  expect(screen.queryByText('当前分支一')).not.toBeInTheDocument()
  view.rerender(<Notice label="分支提示" scope="s1:b2">当前分支二</Notice>)
  expect(screen.getByText('当前分支二')).toBeInTheDocument()
})
