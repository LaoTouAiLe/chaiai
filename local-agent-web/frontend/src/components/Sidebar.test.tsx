import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Sidebar, type SidebarActions } from './Sidebar'

function renderSidebar(
  overrides: Partial<SidebarActions> = {},
  addWorkspace?: { pending: boolean; error: string | null },
) {
  const actions: SidebarActions = {
    onSelectWorkspace: vi.fn(),
    onAddWorkspace: vi.fn(),
    onSelectSession: vi.fn(),
    onNewSession: vi.fn(),
    onOpenSettings: vi.fn(),
    onToggleCollapse: vi.fn(),
    ...overrides,
  }
  return render(<Sidebar workspaces={[]} actions={actions} addWorkspace={addWorkspace} />)
}

describe('Sidebar 添加工作区', () => {
  it('入口只有一条，点了就拉起宿主的目录选择器', () => {
    // 系统对话框自带"新建文件夹"，所以不该再有"按名称创建"的第二条路径。
    const onAddWorkspace = vi.fn()
    renderSidebar({ onAddWorkspace })

    fireEvent.click(screen.getByRole('button', { name: '添加工作区' }))

    expect(onAddWorkspace).toHaveBeenCalledTimes(1)
  })

  it('选择器已经打开时禁用入口，不让第二次点击拉起第二个对话框', () => {
    renderSidebar({}, { pending: true, error: null })

    expect(screen.getByRole('button', { name: '添加工作区' })).toBeDisabled()
  })

  it('失败信息就地显示，且入口保持可再点一次', () => {
    renderSidebar({}, { pending: false, error: '没能打开目录选择器。重试一次即可。' })

    expect(screen.getByRole('status')).toHaveTextContent('没能打开目录选择器')
    expect(screen.getByRole('button', { name: '添加工作区' })).toBeEnabled()
  })

  it('没事发生时不占位', () => {
    renderSidebar({}, { pending: false, error: null })

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

it('搜索会话会筛选侧栏，并能清空恢复', () => {
  render(<Sidebar workspaces={[{
    id: 'workspace-1', title: '项目', sessions: [
      { id: 'session-1', title: '解释 README', status: 'completed', timeLabel: '', selected: false },
      { id: 'session-2', title: '修复测试', status: 'idle', timeLabel: '', selected: false },
    ],
  }]} />)
  const search = screen.getByRole('searchbox', { name: '搜索会话' })
  fireEvent.change(search, { target: { value: 'README' } })
  expect(screen.getByText('解释 README')).toBeInTheDocument()
  expect(screen.queryByText('修复测试')).not.toBeInTheDocument()
  fireEvent.change(search, { target: { value: '不存在' } })
  expect(screen.getByText('没有匹配的会话')).toBeInTheDocument()
  fireEvent.change(search, { target: { value: '' } })
  expect(screen.getByText('修复测试')).toBeInTheDocument()
})
