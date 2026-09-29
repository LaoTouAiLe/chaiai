import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { approvalExpiredScenario, approvalScenario } from '../../fixtures/scenarios'
import type { ApprovalView } from '../../fixtures/types'
import { ApprovalCard } from './ApprovalCard'

function approvalOf(scenario: typeof approvalScenario): ApprovalView {
  if (scenario.takeover.kind !== 'approval') throw new Error('scenario has no approval takeover')
  return scenario.takeover.approval
}

describe('ApprovalCard', () => {
  it('shows the action, its normalised target, the impact and the risk before any button', () => {
    const approval = approvalOf(approvalScenario)
    render(<ApprovalCard approval={approval} />)
    expect(screen.getByText('等待审批')).toBeInTheDocument()
    expect(screen.getByText('delete_file')).toBeInTheDocument()
    expect(screen.getByText('dist/')).toBeInTheDocument()
    expect(screen.getByText('高风险')).toBeInTheDocument()
    for (const item of approval.impact) {
      expect(screen.getByText(item)).toBeInTheDocument()
    }
  })

  it('offers a one-shot decision while pending', () => {
    render(<ApprovalCard approval={approvalOf(approvalScenario)} />)
    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '进入二次确认' })).toBeInTheDocument()
    // Approval is never a permanent grant.
    expect(screen.getByText('批准只对本次调用生效，不会加入免审名单。')).toBeInTheDocument()
  })

  it('drops the buttons once the request is no longer actionable', () => {
    const expired = approvalOf(approvalExpiredScenario)
    render(<ApprovalCard approval={expired} />)
    expect(screen.getByText('审批已失效')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '拒绝' })).not.toBeInTheDocument()
    expect(screen.getByText(/该审批已超时失效，未执行任何操作/)).toBeInTheDocument()
  })

  it('reports a taken decision instead of asking again', () => {
    const base = approvalOf(approvalScenario)
    for (const state of ['executing', 'approved', 'rejected'] as const) {
      const { unmount } = render(<ApprovalCard approval={{ ...base, state, note: null }} />)
      expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
      expect(screen.getByText(/当前状态：/)).toBeInTheDocument()
      unmount()
    }
  })

  it('keeps the card free of secrets', () => {
    const { container } = render(<ApprovalCard approval={approvalOf(approvalScenario)} />)
    const text = container.textContent ?? ''
    expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
  })

  describe('real wiring (T32)', () => {
    it('submits the decision the user made', async () => {
      const onApprove = vi.fn()
      const onReject = vi.fn()
      const user = userEvent.setup()
      render(
        <ApprovalCard
          approval={approvalOf(approvalScenario)}
          actions={{ onApprove, onReject, busy: false }}
        />,
      )

      await user.click(screen.getByRole('button', { name: '进入二次确认' }))
      expect(onApprove).toHaveBeenCalledExactlyOnceWith(false)
      expect(onReject).not.toHaveBeenCalled()

      await user.click(screen.getByRole('button', { name: '拒绝' }))
      expect(onReject).toHaveBeenCalledTimes(1)
    })

    it('locks both buttons the moment a decision is in flight', async () => {
      const onApprove = vi.fn()
      const user = userEvent.setup()
      render(
        <ApprovalCard
          approval={approvalOf(approvalScenario)}
          actions={{ onApprove, onReject: vi.fn(), busy: true }}
        />,
      )

      const approve = screen.getByRole('button', { name: '进入二次确认' })
      const reject = screen.getByRole('button', { name: '拒绝' })
      expect(approve).toBeDisabled()
      expect(reject).toBeDisabled()
      // 锁定期间连点也不该再发第二次决定。
      await user.click(approve)
      expect(onApprove).not.toHaveBeenCalled()
      expect(screen.getByText(/正在提交/)).toBeInTheDocument()
    })

    it('shows the redacted parameters the decision applies to', () => {
      const approval: ApprovalView = {
        ...approvalOf(approvalScenario),
        target: '',
        impact: [],
        parameters: [
          { name: 'path', value: 'dist' },
          { name: 'recursive', value: 'True' },
        ],
      }
      render(<ApprovalCard approval={approval} />)

      expect(screen.getByText('参数')).toBeInTheDocument()
      expect(screen.getByText('path=dist')).toBeInTheDocument()
      expect(screen.getByText('recursive=True')).toBeInTheDocument()
      // 没有目标、没有影响清单时，不再渲染空的「目标 / 影响」行。
      expect(screen.queryByText('目标')).not.toBeInTheDocument()
      expect(screen.queryByText('影响')).not.toBeInTheDocument()
    })

    it('renders no parameters block when the contract carried none', () => {
      render(<ApprovalCard approval={approvalOf(approvalScenario)} />)
      expect(screen.queryByText('参数')).not.toBeInTheDocument()
    })

    it('requires an explicit checkbox on the second recursive-delete approval', async () => {
      const onApprove = vi.fn()
      const user = userEvent.setup()
      render(<ApprovalCard approval={{ ...approvalOf(approvalScenario), confirmationStage: 2 }}
        actions={{ onApprove, onReject: vi.fn(), busy: false }} />)

      const approve = screen.getByRole('button', { name: '确认删除' })
      expect(approve).toBeDisabled()
      await user.click(screen.getByRole('checkbox', { name: '我确认删除该目录及其全部内容' }))
      expect(approve).toBeEnabled()
      await user.click(approve)
      expect(onApprove).toHaveBeenCalledExactlyOnceWith(true)
    })

    it('does not carry the checkbox to another approval or target', async () => {
      const user = userEvent.setup()
      const base = { ...approvalOf(approvalScenario), confirmationStage: 2 as const }
      const actions = { onApprove: vi.fn(), onReject: vi.fn(), busy: false }
      const { rerender } = render(<ApprovalCard approval={base} actions={actions} />)
      await user.click(screen.getByRole('checkbox'))
      expect(screen.getByRole('button', { name: '确认删除' })).toBeEnabled()

      rerender(<ApprovalCard approval={{ ...base, approvalId: 'next-approval' }} actions={actions} />)
      expect(screen.getByRole('checkbox')).not.toBeChecked()
      expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled()

      await user.click(screen.getByRole('checkbox'))
      rerender(<ApprovalCard approval={{ ...base, approvalId: 'next-approval', target: 'other/' }} actions={actions} />)
      expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled()
    })
  })
})
