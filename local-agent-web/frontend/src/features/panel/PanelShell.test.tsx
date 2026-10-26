import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { PanelShell } from './PanelShell'

const mocks = vi.hoisted(() => ({
  retry: vi.fn(), fileError: false, filePending: false, changesError: false,
  changedPaths: [] as string[],
  closeBrowser: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), closeBrowser: mocks.closeBrowser }))
vi.mock('../../api/queries', () => ({
  useSideChatCreation: () => ({ create: () => Promise.resolve('side'), forget: () => {} }),
  useWorkspaceFiles: () => ({ data: { entries: [{ path: 'README.md', name: 'README.md', kind: 'file', size_bytes: 40 }] },
    isError: mocks.fileError, isPending: mocks.filePending, refetch: mocks.retry }),
  useSearchWorkspaceFiles: () => ({ data: { entries: [] }, isFetching: false, refetch: mocks.retry }),
  useArtifactsDetail: () => ({ data: { kind: 'text', text: 'preview' } }),
  useSessionChanges: () => ({ data: { turns: [], unrecorded: mocks.changedPaths.map(path => ({ path })) }, isPending: false, isError: mocks.changesError, refetch: mocks.retry }),
}))
vi.mock('../code-changes/ChangesPanel', () => ({ FileDiff: ({ path }: { path: string }) => <div>对比 {path}</div> }))
vi.mock('./SideChatPane', () => ({ SideChatPane: () => <div>侧聊内容</div> }))
vi.mock('./TerminalPane', () => ({ default: () => <div>终端内容</div> }))
vi.mock('./BrowserPane', () => ({ default: ({ requestId }: { requestId: string }) => <div data-request-id={requestId}>浏览器内容</div> }))
vi.mock('./useHighlight', () => ({ languageOf: () => 'text', useHighlight: () => null }))

const props = { workspaceId: 'w1', sessionId: 's1', trace: { rows: [], calls: [], turns: [] }, onClose: vi.fn() }
const choose = (label: string) => fireEvent.click(screen.getByRole('button', { name: label }))

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
  mocks.fileError = false
  mocks.filePending = false
  mocks.changesError = false
  mocks.changedPaths = []
  document.documentElement.dataset.motion = 'reduced'
})

it('switches tabs with arrow keys, closes the active tab to its neighbour and returns to home', () => {
  render(<PanelShell {...props} />)
  choose('步骤')
  fireEvent.click(screen.getByRole('button', { name: '新建页签' }))
  choose('改动')
  const changes = screen.getByRole('tab', { name: '改动' })
  fireEvent.keyDown(changes, { key: 'ArrowLeft' })
  expect(screen.getByRole('tab', { name: '步骤' })).toHaveFocus()
  fireEvent.keyDown(screen.getByRole('tab', { name: '步骤' }), { key: 'Delete' })
  expect(changes).toHaveFocus()
  expect(changes).toHaveAttribute('aria-selected', 'true')
  fireEvent.click(screen.getByRole('button', { name: '关闭改动页' }))
  expect(screen.getByRole('button', { name: '工作面板首页' })).toHaveFocus()
  expect(screen.getByRole('navigation', { name: '工作面板功能' })).toBeVisible()
})

it('resizes by keyboard within bounds and restores the saved home and width after remounting', () => {
  const view = render(<PanelShell {...props} />)
  const resize = screen.getByRole('separator', { name: '拖动调整面板宽度' })
  fireEvent.keyDown(resize, { key: 'Home' })
  expect(resize).toHaveAttribute('aria-valuenow', '320')
  fireEvent.keyDown(resize, { key: 'ArrowRight' })
  expect(resize).toHaveAttribute('aria-valuenow', '320')
  fireEvent.keyDown(resize, { key: 'End' })
  fireEvent.keyDown(resize, { key: 'ArrowLeft' })
  expect(resize).toHaveAttribute('aria-valuenow', '760')
  choose('步骤')
  fireEvent.click(screen.getByRole('button', { name: '工作面板首页' }))
  view.unmount()
  render(<PanelShell {...props} />)
  expect(screen.getByRole('separator', { name: '拖动调整面板宽度' })).toHaveAttribute('aria-valuenow', '760')
  expect(screen.getByRole('navigation', { name: '工作面板功能' })).toBeVisible()
})

it('separates restored tabs by session and uses a filename as the tab title', () => {
  localStorage.setItem('panel.state.v1:s1', JSON.stringify({ version: 1,
    tabs: [{ id: 'files-1', kind: 'files', param: 'src/main.ts' }], activeId: 'files-1', width: 520 }))
  const view = render(<PanelShell {...props} />)
  expect(screen.getByRole('tab', { name: 'main.ts' })).toHaveAttribute('title', 'src/main.ts')
  expect(screen.getByText('preview')).toBeVisible()
  view.unmount()
  render(<PanelShell {...props} sessionId="s2" />)
  expect(screen.queryByRole('tab')).not.toBeInTheDocument()
})

it('offers retry without claiming an empty file tree while loading or failed', () => {
  mocks.filePending = true
  const view = render(<PanelShell {...props} />)
  choose('文件')
  expect(screen.getByRole('status')).toHaveTextContent('正在读取')
  expect(screen.queryByRole('button', { name: /README.md/ })).not.toBeInTheDocument()
  mocks.filePending = false
  mocks.fileError = true
  view.rerender(<PanelShell {...props} />)
  expect(screen.getByRole('alert')).toHaveTextContent('文件树读取失败')
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(mocks.retry).toHaveBeenCalledOnce()
})

it('distinguishes changes failure from an empty successful result', () => {
  mocks.changesError = true
  const view = render(<PanelShell {...props} />)
  choose('改动')
  expect(screen.getByRole('alert')).toHaveTextContent('改动记录读取失败')
  expect(screen.queryByText('还没有文件改动')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(mocks.retry).toHaveBeenCalledOnce()
  mocks.changesError = false
  view.rerender(<PanelShell {...props} />)
  expect(screen.getByText('还没有文件改动')).toBeVisible()
})

it('closes from panel navigation with Escape and returns focus to the trigger', () => {
  const trigger = document.createElement('button')
  document.body.append(trigger)
  trigger.focus()
  const view = render(<PanelShell {...props} />)
  fireEvent.keyDown(screen.getByRole('button', { name: '工作面板首页' }), { key: 'Escape' })
  expect(props.onClose).toHaveBeenCalledOnce()
  expect(trigger).toHaveFocus()
  view.unmount()
  trigger.remove()
})

it('unmounts panel contents after closing and restores tabs when reopened', async () => {
  const view = render(<PanelShell {...props} />)
  choose('步骤')
  view.rerender(<PanelShell {...props} open={false} />)
  await waitFor(() => expect(screen.queryByRole('complementary', { hidden: true })).not.toBeInTheDocument())
  view.rerender(<PanelShell {...props} open />)
  expect(screen.getByRole('tab', { name: '步骤' })).toHaveAttribute('aria-selected', 'true')
})

it('keeps side chat unavailable until a parent session is selected', () => {
  render(<PanelShell {...props} sessionId={null} />)
  expect(screen.getByRole('button', { name: '侧边对话' })).toBeDisabled()
  expect(screen.getByText('先打开一个主会话，再开启侧边对话')).toBeVisible()
})

it('opens different file references in their own tabs and reuses a normalized existing path', () => {
  const view = render(<PanelShell {...props} />)
  view.rerender(<PanelShell {...props} focus={{ tab: 'files', path: 'E:/repo/a.ts' }} />)
  expect(screen.getByRole('tab', { name: 'a.ts' })).toHaveAttribute('aria-selected', 'true')
  view.rerender(<PanelShell {...props} focus={{ tab: 'files', path: 'E:/repo/b.py' }} />)
  expect(screen.getAllByRole('tab')).toHaveLength(2)
  view.rerender(<PanelShell {...props} focus={{ tab: 'files', path: 'e:\\repo\\a.ts' }} />)
  expect(screen.getAllByRole('tab')).toHaveLength(2)
  expect(screen.getByRole('tab', { name: 'a.ts' })).toHaveAttribute('aria-selected', 'true')
})

it('keeps hash and literal percent filenames in separate tabs', () => {
  const view = render(<PanelShell {...props} />)
  for (const path of ['E:/repo/name#one.ts', 'E:/repo/name#two.ts', 'E:/repo/name%20part.ts', 'E:/repo/name part.ts']) {
    view.rerender(<PanelShell {...props} focus={{ tab: 'files', path }} />)
  }
  expect(screen.getAllByRole('tab')).toHaveLength(4)
  view.rerender(<PanelShell {...props} focus={{ tab: 'files', path: 'E:/repo/name#one.ts' }} />)
  expect(screen.getAllByRole('tab')).toHaveLength(4)
  expect(screen.getByRole('tab', { name: 'name#one.ts' })).toHaveAttribute('aria-selected', 'true')
})

it('keeps the changes directory and filter while selecting, replacing and closing a diff', () => {
  mocks.changedPaths = ['src/main.ts', 'README.md']
  render(<PanelShell {...props} />)
  choose('改动')
  const directory = screen.getByRole('complementary', { name: '改动目录' })
  choose('README.md')
  expect(screen.getByRole('region', { name: '改动详情' })).toHaveTextContent('对比 README.md')
  expect(directory).toBeVisible()
  fireEvent.change(screen.getByRole('searchbox', { name: '按路径筛选改动' }), { target: { value: 'main' } })
  choose('main.ts')
  expect(screen.getByText('对比 src/main.ts')).toBeVisible()
  expect(screen.getByRole('searchbox')).toHaveValue('main')
  expect(screen.queryByRole('button', { name: 'README.md' })).not.toBeInTheDocument()
  choose('关闭改动详情')
  expect(screen.getByText('选择一个改动文件')).toBeVisible()
  expect(directory).toBeVisible()
  const resize = screen.getByRole('separator', { name: '拖动调整改动目录宽度' })
  fireEvent.keyDown(resize, { key: 'ArrowLeft' })
  expect(resize).toHaveAttribute('aria-valuenow', '236')
})

it('slides according to tab order when switching and closing tabs', () => {
  render(<PanelShell {...props} />)
  choose('文件')
  choose('新建页签')
  choose('改动')
  expect(screen.getByRole('tabpanel')).toHaveAttribute('data-slide-direction', 'right')
  fireEvent.keyDown(screen.getByRole('tab', { name: '改动' }), { key: 'ArrowLeft' })
  expect(screen.getByRole('tabpanel')).toHaveAttribute('data-slide-direction', 'left')
  fireEvent.keyDown(screen.getByRole('tab', { name: '文件' }), { key: 'ArrowRight' })
  expect(screen.getByRole('tabpanel')).toHaveAttribute('data-slide-direction', 'right')
  choose('关闭改动页')
  expect(screen.getByRole('tabpanel')).toHaveAttribute('data-slide-direction', 'left')
})

it('restores a browser key but never reuses a closed key after reloading an empty panel', async () => {
  let view = render(<PanelShell {...props} />)
  choose('浏览器')
  const first = (await screen.findByText('浏览器内容')).getAttribute('data-request-id')
  view.unmount()
  view = render(<PanelShell {...props} />)
  expect((await screen.findByText('浏览器内容')).getAttribute('data-request-id')).toBe(first)
  choose('关闭浏览器页')
  expect(mocks.closeBrowser).toHaveBeenCalledWith(first)
  view.unmount()
  render(<PanelShell {...props} />)
  choose('浏览器')
  expect((await screen.findByText('浏览器内容')).getAttribute('data-request-id')).not.toBe(first)
})
