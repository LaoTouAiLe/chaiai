import { useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { EditorDialog } from './EditorDialog'

function Host() {
  const [open, setOpen] = useState(false)
  return <><button onClick={() => setOpen(true)}>编辑连接</button>
    {open ? <EditorDialog title="编辑模型连接" onClose={() => setOpen(false)}>
      <label>名称<input /></label><button>保存</button>
    </EditorDialog> : null}</>
}

describe('EditorDialog', () => {
  it('opens outside the source layout and restores keyboard focus on Escape', async () => {
    const user = userEvent.setup()
    const { container } = render(<Host />)
    const trigger = screen.getByRole('button', { name: '编辑连接' })
    await user.click(trigger)
    const dialog = screen.getByRole('dialog', { name: '编辑模型连接' })
    expect(container).not.toContainElement(dialog)
    expect(screen.getByLabelText('名称')).toHaveFocus()
    expect(document.body.style.overflow).toBe('hidden')
    await user.keyboard('{Escape}')
    expect(dialog).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    expect(document.body.style.overflow).not.toBe('hidden')
  })

  it('keeps Tab inside the editor and blocks dismissing an in-flight save', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    const { rerender } = render(<EditorDialog title="编辑" onClose={onClose}>
      <input aria-label="名称" /><button>保存</button>
    </EditorDialog>)
    screen.getByRole('button', { name: '保存' }).focus()
    await user.tab()
    expect(screen.getByRole('button', { name: '关闭编辑' })).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByRole('button', { name: '保存' })).toHaveFocus()
    rerender(<EditorDialog title="编辑" onClose={onClose} dismissible={false}><input aria-label="名称" /></EditorDialog>)
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '关闭编辑' })).toBeDisabled()
    expect(onClose).not.toHaveBeenCalled()
    // Disabling the active save button can leave focus on body in Chromium.
    rerender(<EditorDialog title="编辑" onClose={onClose}><input aria-label="名称" /></EditorDialog>)
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
