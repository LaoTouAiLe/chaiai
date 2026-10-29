import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Sidebar, type SidebarActions } from './Sidebar'

function renderSidebar(
  overrides: Partial<SidebarActions> = {},
  addWorkspace?: { pending: boolean; error: string | null },
  collapsed = false,
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
  return render(<Sidebar workspaces={[]} actions={actions} addWorkspace={addWorkspace} collapsed={collapsed} />)
}

describe('Sidebar 添加工作区', () => {
  it('侧栏底部设置入口复用已有动作，收起后仍可打开', () => {
    const onOpenSettings = vi.fn()
    const actions: SidebarActions = {
      onSelectWorkspace: vi.fn(), onAddWorkspace: vi.fn(), onSelectSession: vi.fn(),
      onNewSession: vi.fn(), onOpenSettings, onToggleCollapse: vi.fn(),
    }
    const view = render(<Sidebar workspaces={[]} actions={actions} />)
    fireEvent.click(screen.getByRole('button', { name: /^设置$/ }))
    view.rerender(<Sidebar workspaces={[]} collapsed actions={actions} />)
    fireEvent.click(screen.getByRole('button', { name: /^设置$/ }))
    expect(onOpenSettings).toHaveBeenCalledTimes(2)
  })

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

it('会话行显示"现在在做什么"，没有状态的行不占位（v0.8 T71；§19.1）', () => {
  render(<Sidebar workspaces={[{
    id: 'workspace-1', title: '项目', sessions: [
      { id: 'session-1', title: '跑着的会话', status: 'running', stateLabel: '正在跑', timeLabel: '', selected: false },
      { id: 'session-2', title: '等你处理的会话', status: 'awaiting-approval', stateLabel: '在等你处理', timeLabel: '', selected: false },
      { id: 'session-3', title: '闲着的会话', status: 'idle', timeLabel: '', selected: false },
    ],
  }]} />)

  // 状态点区分不开"正在跑"与"等你处理"（同一个 warn 令牌），所以可见措辞必须自己说清楚。
  expect(screen.getByText('正在跑')).toBeInTheDocument()
  expect(screen.getByText('在等你处理')).toBeInTheDocument()
  // 屏幕阅读器读的是那份隐藏文本（点本身是装饰）。
  expect(screen.getByText('运行中')).toHaveClass('visually-hidden')
  expect(screen.getByText('等待审批')).toHaveClass('visually-hidden')
  // 没有状态的行只有"空闲"，不编一个状态点。
  expect(screen.getByText('空闲')).toHaveClass('visually-hidden')
  expect(screen.queryByText('需人工核对')).not.toBeInTheDocument()
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

it('状态筛选与搜索共同生效，切回全部保留原始会话', () => {
  render(<Sidebar workspaces={[{ id: 'w', title: '项目', sessions: [
    { id: 'running', title: '运行测试', status: 'running', timeLabel: '', selected: false },
    { id: 'approval', title: '审批改动', status: 'awaiting-approval', timeLabel: '', selected: false },
    { id: 'input', title: '回答问题', status: 'awaiting-input', timeLabel: '', selected: false },
    { id: 'done', title: '已结束的任务', status: 'completed', timeLabel: '', selected: false },
  ] }]} />)
  fireEvent.click(screen.getByRole('button', { name: '进行中的会话' }))
  expect(screen.getByText('运行测试')).toBeInTheDocument()
  expect(screen.queryByText('审批改动')).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索会话' }), { target: { value: '审批' } })
  expect(screen.getByText('没有匹配的会话')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '等待处理的会话' }))
  expect(screen.getByText('审批改动')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索会话' }), { target: { value: '' } })
  expect(screen.getByText('回答问题')).toBeInTheDocument()
  expect(screen.getByText('等待回答')).toHaveClass('visually-hidden')
  fireEvent.click(screen.getByRole('button', { name: '全部会话' }))
  expect(screen.getByText('已结束的任务')).toBeInTheDocument()
})

it('独立功能导航在目录收起后仍保留返回会话与任务入口', () => {
  const onOpenConversation = vi.fn()
  const onOpenTasks = vi.fn()
  renderSidebar({ onOpenConversation, onOpenTasks }, undefined, true)
  fireEvent.click(screen.getByRole('button', { name: '会话工作台' }))
  fireEvent.click(screen.getByRole('button', { name: '任务中心' }))
  expect(onOpenConversation).toHaveBeenCalledOnce()
  expect(onOpenTasks).toHaveBeenCalledOnce()
})

it('每个工作区独立展示更多，保留选中项且搜索全部会话', () => {
  render(<Sidebar workspaces={['甲', '乙'].map((title) => ({ id: title, title,
    sessions: Array.from({ length: 8 }, (_, index) => ({ id: `${title}-${index}`, title: `${title}会话${index}`,
      status: 'idle' as const, timeLabel: '', selected: title === '甲' && index === 7 })),
  }))} />)
  expect(screen.queryByText('甲会话5')).not.toBeInTheDocument()
  expect(screen.getByText('甲会话7')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '甲展示更多会话' }))
  expect(screen.getByText('甲会话5')).toBeInTheDocument()
  expect(screen.queryByText('乙会话5')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '甲收起会话' }))
  expect(screen.queryByText('甲会话5')).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索会话' }), { target: { value: '乙会话6' } })
  expect(screen.getByText('乙会话6')).toBeInTheDocument()
})
