import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
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
    // The outcome stays visible ...
    expect(screen.getByText('审批已失效')).toBeInTheDocument()
    expect(screen.getByText(/该审批已超时失效，未执行任何操作/)).toBeInTheDocument()
    // ... and the user can issue a new request again.
    expect(screen.getByRole('textbox', { name: '给智能体发消息' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '发送消息' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
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
