import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { SidebarItemMenu } from './SidebarItemMenu'

describe('侧栏管理菜单', () => {
  it('保存失败保留名称，重试成功后关闭表单', async () => {
    const rename = vi.fn().mockRejectedValueOnce(new ApiError({ status: 409, code: 'SESSION_BUSY', message: '会话正忙' })).mockResolvedValueOnce(undefined)
    render(<SidebarItemMenu title="原名称" kind="会话" onRename={rename} onRemove={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /更多操作/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: '重命名' }))
    fireEvent.change(screen.getByRole('textbox', { name: '名称' }), { target: { value: '新名称' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('会话正忙')
    expect(screen.getByRole('textbox')).toHaveValue('新名称')
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(rename).toHaveBeenLastCalledWith('新名称')
  })

  it('工作区确认移除说明保留文件，取消不发送请求', async () => {
    const remove = vi.fn().mockResolvedValue(undefined)
    render(<SidebarItemMenu title="项目" kind="工作区" onRename={vi.fn()} onRemove={remove} />)
    fireEvent.click(screen.getByRole('button', { name: /更多操作/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: '移除工作区' }))
    expect(screen.getByRole('dialog')).toHaveTextContent('文件保留在原目录')
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(remove).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /更多操作/ }))
    fireEvent.click(screen.getByRole('menuitem', { name: '移除工作区' }))
    fireEvent.click(screen.getByRole('button', { name: '确认移除' }))
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1))
  })

  it('键盘可移动与关闭菜单，忙碌会话不能归档', () => {
    render(<SidebarItemMenu title="运行" kind="会话" blocked onRename={vi.fn()} onRemove={vi.fn()} />)
    const trigger = screen.getByRole('button', { name: /更多操作/ })
    fireEvent.click(trigger)
    expect(screen.getByRole('menuitem', { name: '归档会话' })).toBeDisabled()
    fireEvent.keyDown(screen.getByRole('menuitem', { name: '重命名' }), { key: 'Escape' })
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('点击菜单之外关闭菜单', () => {
    render(<SidebarItemMenu title="原名称" kind="会话" onRename={vi.fn()} onRemove={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: /更多操作/ }))
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })
})
