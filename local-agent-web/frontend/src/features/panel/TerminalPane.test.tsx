import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { TerminalDto } from '../../api/client'
import TerminalPane from './TerminalPane'

const mocks = vi.hoisted(() => ({ list: vi.fn(), request: vi.fn(), decide: vi.fn(), stop: vi.fn(), construct: vi.fn() }))
vi.mock('../../api/client', () => ({ listTerminals: mocks.list, requestTerminal: mocks.request,
  decideTerminal: mocks.decide, stopTerminal: mocks.stop }))
vi.mock('@xterm/xterm', () => ({ Terminal: mocks.construct }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: vi.fn() }))

const pending: TerminalDto = { approval_id: 'approval', terminal_id: null, workspace_id: 'workspace',
  cwd: 'E:\\project', shell: 'C:\\Windows\\System32\\cmd.exe', permission_mode: 'workspace-write',
  status: 'pending', expires_at: '2026-10-23T10:05:00Z' }

beforeEach(() => {
  vi.clearAllMocks()
  mocks.list.mockResolvedValue({ available: true, items: [] })
  mocks.request.mockResolvedValue(pending)
})

it('opens a reviewable approval without automatically starting or approving a process', async () => {
  render(<TerminalPane workspaceId="workspace" />)
  await waitFor(() => expect(screen.getByRole('button', { name: '新建' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '新建' }))
  expect(await screen.findByText('批准启动终端')).toBeVisible()
  expect(screen.getByText(pending.cwd)).toBeVisible()
  expect(screen.getByText(/输入的命令不再逐条审批/)).toBeVisible()
  expect(mocks.request).toHaveBeenCalledWith('workspace')
  expect(mocks.decide).not.toHaveBeenCalled()
  expect(mocks.construct).not.toHaveBeenCalled()
})

it('shows read-only scope and sends an explicit rejection', async () => {
  mocks.list.mockResolvedValue({ available: true, items: [{ ...pending, permission_mode: 'read-only' }] })
  mocks.decide.mockResolvedValue({ ...pending, status: 'rejected' })
  render(<TerminalPane workspaceId="workspace" />)
  expect(await screen.findByText('当前为只读模式，不允许写入文件。')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
  await waitFor(() => expect(mocks.decide).toHaveBeenCalledWith('approval', 'reject'))
  expect(mocks.construct).not.toHaveBeenCalled()
})

it('reports a failed launch without claiming a connected terminal', async () => {
  mocks.list.mockResolvedValue({ available: true, items: [pending] })
  mocks.decide.mockResolvedValue({ ...pending, status: 'failed', detail: '无法建立写入沙箱' })
  render(<TerminalPane workspaceId="workspace" />)
  fireEvent.click(await screen.findByRole('button', { name: '批准并启动' }))
  expect(await screen.findByText(/启动失败 · 无法建立写入沙箱/)).toBeVisible()
  expect(mocks.decide).toHaveBeenCalledWith('approval', 'approve')
  expect(mocks.construct).not.toHaveBeenCalled()
})

it('keeps the unsupported-platform action disabled', async () => {
  mocks.list.mockResolvedValue({ available: false, reason: '此平台不支持受控终端', items: [] })
  render(<TerminalPane workspaceId="workspace" />)
  expect(await screen.findByText('此平台不支持受控终端')).toBeVisible()
  expect(screen.getByRole('button', { name: '新建' })).toBeDisabled()
})
