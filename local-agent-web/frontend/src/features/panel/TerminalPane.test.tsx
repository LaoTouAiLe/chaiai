// 一个标签 = 一个终端（2026-10-28）。
//
// 这组用例盯的是"界面上还有没有审批/新建/结束"，以及"连的是哪一条终端"：
// 终端由 PanelShell 在打开标签时创建，本组件只负责接上那一个 terminalId。

import { act, render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

import TerminalPane from './TerminalPane'

const terminal = {
  open: vi.fn(), loadAddon: vi.fn(), focus: vi.fn(), reset: vi.fn(), write: vi.fn(),
  dispose: vi.fn(), onData: vi.fn(() => ({ dispose: vi.fn() })), cols: 80, rows: 24,
}

// 用普通函数而不是箭头函数：组件里是 `new Terminal(...)`，箭头函数不能被 new。
vi.mock('@xterm/xterm', () => ({ Terminal: vi.fn(function () { return terminal }) }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: vi.fn(function () { return { fit: vi.fn() } }) }))

class FakeSocket {
  static OPEN = 1
  static last: FakeSocket | null = null
  static instances: FakeSocket[] = []
  readyState = 0
  binaryType = 'blob'
  bufferedAmount = 0
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { reason: string }) => void) | null = null
  onerror: (() => void) | null = null
  sent: string[] = []
  constructor(public url: string) {
    FakeSocket.last = this
    FakeSocket.instances.push(this)
  }
  send(payload: string) { this.sent.push(payload) }
  close() { this.readyState = 3 }
}

beforeEach(() => {
  vi.clearAllMocks()
  FakeSocket.last = null
  FakeSocket.instances = []
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
})

it('connects to exactly one terminal and offers no approval step', () => {
  render(<TerminalPane terminalId="terminal-a" />)

  // 组件把 `URL` 对象直接交给 WebSocket，断言时先转成字符串。
  expect(String(FakeSocket.last?.url)).toContain('/api/terminals/terminal-a/stream')
  // 审批卡、新建、结束都不应该再出现。
  expect(screen.queryByText(/批准启动终端/)).toBeNull()
  expect(screen.queryByRole('button', { name: '新建' })).toBeNull()
  expect(screen.queryByRole('button', { name: '结束' })).toBeNull()
  expect(screen.queryByRole('button', { name: '批准并启动' })).toBeNull()
})

it('reports a connected terminal when the socket opens', () => {
  render(<TerminalPane terminalId="terminal-a" />)

  act(() => {
    const socket = FakeSocket.last!
    socket.readyState = FakeSocket.OPEN
    socket.onopen?.()
  })

  expect(screen.getByRole('status')).toHaveTextContent('已连接')
})

it('reports the exit code instead of pretending it is still running', () => {
  render(<TerminalPane terminalId="terminal-a" />)

  act(() => {
    FakeSocket.last!.onmessage?.({
      data: JSON.stringify({ type: 'exit', terminal: { exit_code: 3 } }),
    })
  })

  expect(screen.getByRole('status')).toHaveTextContent('终端已结束（退出码 3）')
})

it('shows the close reason when the socket drops', () => {
  render(<TerminalPane terminalId="terminal-a" />)

  act(() => {
    FakeSocket.last!.onclose?.({ reason: '终端不存在或服务已重启。' })
  })

  expect(screen.getByRole('status')).toHaveTextContent('终端不存在或服务已重启。')
})

it('does not spawn a second socket when the status re-renders', () => {
  const { rerender } = render(<TerminalPane terminalId="terminal-a" />)
  act(() => { FakeSocket.last!.onmessage?.({ data: JSON.stringify({ type: 'reset' }) }) })
  rerender(<TerminalPane terminalId="terminal-a" />)

  expect(FakeSocket.instances).toHaveLength(1)
})
