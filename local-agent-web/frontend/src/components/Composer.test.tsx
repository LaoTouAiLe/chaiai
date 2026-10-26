import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import {
  approvalExpiredScenario,
  approvalScenario,
  emptyNoWorkspaceScenario,
  runningEmptyScenario,
  shellScenario,
} from '../fixtures/scenarios'
import type { ConversationScenario } from '../fixtures/types'
import { Composer } from './Composer'

function renderComposer(scenario: ConversationScenario) {
  return render(
    <Composer
      session={scenario.session}
      plan={scenario.plan}
      meter={scenario.context}
      takeover={scenario.takeover}
      modelMenu={scenario.modelMenu}
      state={scenario.composer}
    />,
  )
}

describe('Composer takeover', () => {
  it('lets a pending approval own the input area', () => {
    renderComposer(approvalScenario)
    expect(screen.getByRole('region', { name: '审批' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '进入二次确认' })).toBeInTheDocument()
    // The chat input must not stay reachable underneath a pending decision.
    expect(screen.queryByRole('textbox', { name: '给智能体发消息' })).not.toBeInTheDocument()
  })

  it('gives the input area back once the request is no longer actionable', () => {
    renderComposer(approvalExpiredScenario)
    // The outcome stays visible — collapsed to one line, so a finished card cannot
    // keep occupying the space above the composer ...
    expect(screen.getByText('审批已失效')).toBeInTheDocument()
    // ... and the user can issue a new request again.
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
  })

  it('shows a different finished approval after the previous one was dismissed', async () => {
    if (approvalExpiredScenario.takeover.kind !== 'approval') throw new Error('fixture mismatch')
    const baseApproval = approvalExpiredScenario.takeover.approval
    const user = userEvent.setup()
    const renderWithApproval = (approvalId: string) => (
      <Composer
        session={approvalExpiredScenario.session}
        plan={approvalExpiredScenario.plan}
        meter={approvalExpiredScenario.context}
        takeover={{
          kind: 'approval',
          approval: { ...baseApproval, approvalId },
        }}
        modelMenu={approvalExpiredScenario.modelMenu}
        state={approvalExpiredScenario.composer}
      />
    )

    const { rerender } = render(renderWithApproval('old-approval'))
    await user.click(screen.getByRole('button', { name: '关闭审批结果' }))
    expect(screen.queryByRole('region', { name: '审批' })).not.toBeInTheDocument()

    rerender(renderWithApproval('new-approval'))
    expect(screen.getByRole('region', { name: '审批' })).toBeInTheDocument()
    expect(screen.getByText('审批已失效')).toBeInTheDocument()
  })

  it('restores the input for every state that no longer needs a decision', () => {
    if (approvalScenario.takeover.kind !== 'approval') throw new Error('fixture mismatch')
    const { approval } = approvalScenario.takeover
    for (const state of ['executing', 'approved', 'rejected'] as const) {
      const { unmount } = render(
        <Composer
          session={approvalScenario.session}
          plan={null}
          meter={approvalScenario.context}
          takeover={{ kind: 'approval', approval: { ...approval, state, note: null } }}
          modelMenu={null}
        />,
      )
      expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeInTheDocument()
      unmount()
    }
  })

  it('keeps the input free when nothing has taken it over', () => {
    renderComposer(shellScenario)
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '审批' })).not.toBeInTheDocument()
  })
})

describe('Composer input state', () => {
  it.each(['running', 'completed', 'failed'] as const)('says nothing about compression: %s', (compression) => {
    // 2026-10-16：压缩提示搬进了**对话流**（`features/conversation/CompressionRow`）——它是流程的一步，
    // 与 Think、工具行同级；挂在输入框上方会让人以为那是"输入状态"。输入区因此不再声称任何压缩状态。
    renderComposer({ ...shellScenario, context: { ...shellScenario.context, compression } })
    expect(screen.queryByText('正在压缩…')).not.toBeInTheDocument()
    expect(screen.queryByText('上下文已自动压缩')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeEnabled()
  })
  it('disables the input and states why when no workspace is selected', () => {
    renderComposer(emptyNoWorkspaceScenario)
    const input = screen.getByRole('textbox', { name: '给智能体发消息' })
    expect(input).toBeDisabled()
    // The reason is a real element, not a placeholder that greying-out hides.
    const reason = screen.getByText(/未选择工作区：发送已禁用/)
    expect(input).toHaveAttribute('aria-describedby', reason.id)
  })

  it('blocks sending while a turn runs without promising a stop control', () => {
    renderComposer(runningEmptyScenario)
    expect(screen.getByText('正在分析你的请求…')).toBeInTheDocument()

    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeDisabled()
    const note = screen.getByText(/任务进行中：可以先继续写草稿/)
    expect(send).toHaveAttribute('aria-describedby', note.id)

    // v0.4 has no cancel semantics: rendering a stop control would promise one.
    expect(screen.queryByRole('button', { name: /停止/ })).not.toBeInTheDocument()
    // Drafting stays possible; only sending is unavailable.
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeEnabled()
  })

  it('stays enabled with no phase when the fixture says nothing', () => {
    renderComposer(shellScenario)
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument()
    expect(screen.queryByText(/任务进行中/)).not.toBeInTheDocument()
  })
})

describe('Composer 追问引用（2026-10-26）', () => {
  function renderQuoted(quote: string | null, onClearQuote = vi.fn()) {
    return render(
      <Composer
        session={shellScenario.session}
        plan={shellScenario.plan}
        meter={shellScenario.context}
        takeover={shellScenario.takeover}
        modelMenu={shellScenario.modelMenu}
        state={shellScenario.composer}
        actions={{ draft: '', onDraftChange: () => {}, onSend: () => true, canSend: false, quote, onClearQuote }}
      />,
    )
  }

  it('shows the quoted text as one line above the input and lets the user drop it', () => {
    const clear = vi.fn()
    renderQuoted('第一段\n\n第二段', clear)

    // 引用可能是整条回答：摆成多行会挤掉输入区，所以压成一行。
    const bar = screen.getByRole('note', { name: '引用内容' })
    expect(bar).toHaveTextContent('第一段 第二段')
    expect(bar).not.toHaveTextContent('\n')

    fireEvent.click(screen.getByRole('button', { name: '取消引用' }))
    expect(clear).toHaveBeenCalledTimes(1)
  })

  it('renders no quote row when nothing is quoted', () => {
    renderQuoted(null)
    expect(screen.queryByRole('note', { name: '引用内容' })).not.toBeInTheDocument()
  })
})

describe('Composer 图片（2026-10-16）', () => {
  /** 真接线（带 `actions`）的渲染：只测与图片有关的那条路。 */
  function renderWired(
    onSend: (ids: readonly string[]) => boolean,
    onAttachFile: (file: File) => Promise<{ id: string; previewUrl: string }>,
  ) {
    return render(
      <Composer
        session={shellScenario.session}
        plan={shellScenario.plan}
        meter={shellScenario.context}
        takeover={shellScenario.takeover}
        modelMenu={shellScenario.modelMenu}
        state={shellScenario.composer}
        actions={{ draft: '', onDraftChange: () => {}, onSend, canSend: false, onAttachFile }}
      />,
    )
  }

  it('queues a pasted image and sends its reference even with an empty draft', async () => {
    // 用户实拍过的那两个坑一起钉住：① 输入区**没有粘贴入口**（粘不进去 ✗）；
    // ② 只贴图不打字就发不出去（`canSend` 只管"草稿非空" ✗）。
    const sent: (readonly string[])[] = []
    const attach = vi.fn(async () => ({
      id: '2026/10/16/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbb.png',
      previewUrl: 'blob:preview',
    }))
    const { container } = renderWired((ids) => {
      sent.push(ids)
      return true
    }, attach)

    const box = screen.getByRole('textbox', { name: '给智能体发消息' })
    const file = new File([new Uint8Array([1, 2, 3])], 'green.png', { type: 'image/png' })
    fireEvent.paste(box, { clipboardData: { files: [file] } })

    await waitFor(() => expect(attach).toHaveBeenCalledWith(file))
    // 待发缩略图出现（可移除）。
    await waitFor(() => expect(container.querySelector('img[src="blob:preview"]')).not.toBeNull())
    expect(screen.queryByRole('button', { name: '移除图片 green.png' })).toBeInTheDocument()

    // 草稿为空也能发（只发图）——提交出去的是**引用**，不是文件本身。
    const send = screen.getByRole('button', { name: '发送消息' })
    expect(send).toBeEnabled()
    fireEvent.click(send)
    expect(sent).toEqual([['2026/10/16/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/bbbbbbbbbbbbbbbb.png']])
  })

  it('keeps the plain-text paste untouched when the clipboard has no image', () => {
    // 只在**真有图**时拦默认行为：否则连正常粘贴文字都会被吃掉。
    const attach = vi.fn(async () => ({ id: 'x', previewUrl: 'blob:x' }))
    renderWired(() => true, attach)

    const box = screen.getByRole('textbox', { name: '给智能体发消息' })
    fireEvent.paste(box, { clipboardData: { files: [] } })
    expect(attach).not.toHaveBeenCalled()
  })

  it('keeps queued images when the submit is refused (2026-10-20)', async () => {
    // 实测缺陷：一轮进行中 / 提交在飞时回车，`onSend` 静默拒绝，但图片已被清空——
    // 用户看到"问题没发出去、文字还在、图片没了"。队列只有在对方**接受**后才清。
    const attach = vi.fn(async () => ({ id: 'kept', previewUrl: 'blob:kept' }))
    const onSend = vi.fn(() => false)
    const { container } = renderWired(onSend, attach)

    const box = screen.getByRole('textbox', { name: '给智能体发消息' })
    const file = new File([new Uint8Array([1, 2, 3])], 'keep.png', { type: 'image/png' })
    fireEvent.paste(box, { clipboardData: { files: [file] } })
    await waitFor(() => expect(container.querySelector('img[src="blob:kept"]')).not.toBeNull())

    fireEvent.click(screen.getByRole('button', { name: '发送消息' }))
    expect(onSend).toHaveBeenCalledTimes(1)
    expect(container.querySelector('img[src="blob:kept"]')).not.toBeNull()
  })
})
