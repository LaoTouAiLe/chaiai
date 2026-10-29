import { useRef, useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { AnchoredPopover } from './AnchoredPopover'

function Host() {
  const trigger = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  return <><button ref={trigger} onClick={() => setOpen(value => !value)}>选择模型</button>
    <input aria-label="草稿" />
    {open ? <AnchoredPopover label="模型选择" anchorRef={trigger} onClose={() => setOpen(false)}>
      <button aria-pressed="true">当前模型</button>
    </AnchoredPopover> : null}</>
}

describe('AnchoredPopover', () => {
  it('uses a portal and returns to its trigger on Escape', async () => {
    const user = userEvent.setup()
    const { container } = render(<Host />)
    const trigger = screen.getByRole('button', { name: '选择模型' })
    await user.click(trigger)
    expect(container).not.toContainElement(screen.getByRole('dialog', { name: '模型选择' }))
    expect(screen.getByRole('button', { name: '当前模型' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('closes when clicking or tabbing back into the conversation', async () => {
    const user = userEvent.setup()
    render(<Host />)
    await user.click(screen.getByRole('button', { name: '选择模型' }))
    await user.click(screen.getByRole('textbox', { name: '草稿' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '草稿' })).toHaveFocus()
    await user.click(screen.getByRole('button', { name: '选择模型' }))
    await user.tab({ shift: true })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
