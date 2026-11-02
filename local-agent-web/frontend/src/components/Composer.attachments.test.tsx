import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { shellScenario } from '../fixtures/scenarios'
import { useAttachmentDrafts } from '../stores/attachments'
import { Composer, type ComposerActions } from './Composer'

beforeEach(() => useAttachmentDrafts.setState({ drafts: {} }))

function host(scope: string, actions: Partial<ComposerActions> = {}) {
  return <Composer session={shellScenario.session} plan={null} meter={shellScenario.context}
    takeover={{ kind: 'none' }} modelMenu={null} queueScope={scope}
    actions={{ draft: '读取文件', onDraftChange: vi.fn(), onSend: () => 'sent', canSend: true,
      onAttachDocument: async file => ({ id: `${scope}/${file.name}`, name: file.name, sizeBytes: file.size }), ...actions }} />
}

function choose(container: HTMLElement, count = 1) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement
  fireEvent.change(input, { target: { files: Array.from({ length: count }, (_, i) => new File(['pdf'], `report-${i}.pdf`, { type: 'application/pdf' })) } })
}

it('keeps delayed uploads with their original session across switching and remounting', async () => {
  let finish!: (file: { id: string; name: string; sizeBytes: number }) => void
  const upload = vi.fn(() => new Promise<{ id: string; name: string; sizeBytes: number }>(resolve => { finish = resolve }))
  const { container, rerender, unmount } = render(host('s1', { onAttachDocument: upload }))
  choose(container)
  expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled()
  rerender(host('s2'))
  finish({ id: 's1/report-0.pdf', name: 'report-0.pdf', sizeBytes: 3 })
  await waitFor(() => expect(useAttachmentDrafts.getState().drafts.s1.items).toHaveLength(1))
  expect(screen.queryByRole('button', { name: '移除附件 report-0.pdf' })).not.toBeInTheDocument()
  unmount()
  render(host('s1'))
  expect(screen.getByRole('button', { name: '移除附件 report-0.pdf' })).toBeInTheDocument()
})

it('blocks clicks and Enter while uploading, then sends the completed reference', async () => {
  let finish!: (file: { id: string; name: string; sizeBytes: number }) => void
  const send = vi.fn<ComposerActions['onSend']>(() => 'sent')
  const { container } = render(host('s1', { onSend: send,
    onAttachDocument: () => new Promise(resolve => { finish = resolve }) }))
  choose(container)
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
  expect(send).not.toHaveBeenCalled()
  finish({ id: 's1/report-0.pdf', name: 'report-0.pdf', sizeBytes: 3 })
  await waitFor(() => expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  expect(send).toHaveBeenCalledWith([], [{ id: 's1/report-0.pdf', name: 'report-0.pdf' }])
})

it('keeps attachments after HTTP rejection and clears only after acceptance', async () => {
  const send = vi.fn<ComposerActions['onSend']>().mockResolvedValueOnce('rejected').mockResolvedValueOnce('sent')
  const { container } = render(host('s1', { onSend: send }))
  choose(container)
  await screen.findByRole('button', { name: '移除附件 report-0.pdf' })
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled())
  expect(screen.getByRole('button', { name: '移除附件 report-0.pdf' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: '移除附件 report-0.pdf' })).not.toBeInTheDocument())
})

it('rejects a sixth document before upload and keeps the five already selected', async () => {
  const upload = vi.fn(async (file: File) => ({ id: file.name, name: file.name, sizeBytes: file.size }))
  const { container } = render(host('s1', { onAttachDocument: upload }))
  choose(container, 5)
  await waitFor(() => expect(screen.getAllByRole('button', { name: /移除附件/ })).toHaveLength(5))
  await waitFor(() => expect(screen.getByRole('button', { name: '添加附件' })).toBeEnabled())
  choose(container)
  expect(upload).toHaveBeenCalledTimes(5)
  expect(screen.getByRole('alert')).toHaveTextContent('最多附带 5 份文档')
  expect(screen.getAllByRole('button', { name: /移除附件/ })).toHaveLength(5)
})
