import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { SideChatPane } from './SideChatPane'
import { GeneralContext, generalDefaults } from '../settings/preferencesContext'

const mock = vi.hoisted(() => ({ send: vi.fn(), retry: vi.fn(), pending: false, snapshotError: false,
  snapshot: { session_id: 'side', title: '侧聊', session_state: 'completed', messages: [], current_answer_streaming: false } }))
vi.mock('../../api/queries', () => ({
  useSnapshot: () => ({ data: mock.snapshot, isPending: false, isError: mock.snapshotError, refetch: mock.retry }),
  useSubmitMessage: () => ({ isPending: mock.pending, mutateAsync: mock.send }),
}))
vi.mock('../../api/useSessionStream', () => ({ useSessionStream: () => ({ reconnecting: false }) }))
vi.mock('../../stores/stream', () => ({ useStreamTail: () => null }))
vi.mock('../conversation/Conversation', () => ({ Conversation: ({ scenario }: { scenario: { activity?: { label: string } } }) => <div>{scenario.activity?.label}</div> }))

function Harness({ ctrlEnter = false }: { ctrlEnter?: boolean }) {
  const [draft, setDraft] = useState('')
  const [visible, setVisible] = useState(true)
  return <GeneralContext value={{ ...generalDefaults, send_shortcut: ctrlEnter ? 'ctrl_enter' : 'enter' }}>
    <button onClick={() => setVisible(value => !value)}>切换面板</button>
    {visible ? <SideChatPane parentSessionId="main" sessionId="side" onStart={() => Promise.resolve('side')} onSession={() => {}}
      draft={draft} onDraftChange={setDraft} /> : <input aria-label="其他位置编辑草稿" value={draft} onChange={event => setDraft(event.target.value)} />}
  </GeneralContext>
}

beforeEach(() => {
  vi.clearAllMocks()
  mock.pending = false
  mock.snapshotError = false
  mock.snapshot.current_answer_streaming = false
  mock.send.mockResolvedValue({ status: 'completed' })
})

it('keeps an offline draft when the local request fails without silently queuing it', async () => {
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false)
  mock.send.mockRejectedValueOnce(new Error('offline'))
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox', { name: '侧边对话输入框' }), { target: { value: '离线草稿' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('发送失败'))
  expect(screen.getByRole('textbox')).toHaveValue('离线草稿')
  expect(mock.send).toHaveBeenCalledOnce()
})

it('restores the failed draft and gives a visible failure instead of losing the message', async () => {
  mock.send.mockRejectedValueOnce(new Error('network'))
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '请求失败草稿' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('发送失败'))
  expect(screen.getByRole('textbox')).toHaveValue('请求失败草稿')
})

it('does not overwrite newer text when a request fails after the pane is unmounted', async () => {
  let reject: (cause: Error) => void = () => {}
  mock.send.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '原始问题' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '更新的草稿' } })
  await act(async () => { reject(new Error('late failure')) })
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  expect(screen.getByRole('textbox')).toHaveValue('更新的草稿')
})

it('restores an empty draft even if the sending pane was unmounted', async () => {
  let reject: (cause: Error) => void = () => {}
  mock.send.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail }))
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '原始问题' } })
  fireEvent.click(screen.getByRole('button', { name: '发送' }))
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  await act(async () => { reject(new Error('late failure')) })
  fireEvent.click(screen.getByRole('button', { name: '切换面板' }))
  expect(screen.getByRole('textbox')).toHaveValue('原始问题')
})

it('respects Chinese IME confirmation and the configured Ctrl+Enter shortcut', () => {
  render(<Harness ctrlEnter />)
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: '中文候选词' } })
  fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true, isComposing: true, keyCode: 229 })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(mock.send).not.toHaveBeenCalled()
  fireEvent.keyDown(input, { key: 'Enter', ctrlKey: true })
  expect(mock.send).toHaveBeenCalledWith({ text: '中文候选词', context: {} })
})

it('blocks keyboard submissions while another request is pending and shows processing', () => {
  mock.pending = true
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '下一条草稿' } })
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
  expect(screen.getByRole('button', { name: '处理中' })).toBeDisabled()
  expect(screen.getByText('正在处理请求')).toBeVisible()
  expect(mock.send).not.toHaveBeenCalled()
})

it('shows a retry for snapshot failure instead of allowing submission against unknown state', () => {
  mock.snapshotError = true
  render(<Harness />)
  fireEvent.change(screen.getByRole('textbox'), { target: { value: '保留草稿' } })
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(mock.retry).toHaveBeenCalledOnce()
  expect(screen.getByRole('textbox')).toHaveValue('保留草稿')
})
