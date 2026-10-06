import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { approvalExpiredScenario, approvalScenario } from '../../fixtures/scenarios'
import type { ApprovalView } from '../../fixtures/types'
import { ApprovalCard } from './ApprovalCard'

function approvalOf(scenario: typeof approvalScenario): ApprovalView {
  if (scenario.takeover.kind !== 'approval') throw new Error('scenario has no approval takeover')
  return scenario.takeover.approval
}

describe('ApprovalCard', () => {
  beforeEach(() => {
    // "已关闭"记在 localStorage 里，用例之间必须隔离：否则一个用例点过关闭，
    // 后面复用同一个 approvalId 的用例就整体不渲染了。
    localStorage.clear()
  })
  it('shows the action, its normalised target, the impact and the risk before any button', () => {
    const approval = approvalOf(approvalScenario)
    render(<ApprovalCard approval={approval} />)
    expect(screen.getByText('等待审批')).toBeInTheDocument()
    expect(screen.getByText('删除文件')).toBeInTheDocument()
    expect(screen.queryByText(approval.approvalId)).not.toBeInTheDocument()
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
    // 终态默认收起：状态本身还在屏幕上，详情要用户点开才看（见下面的展开用例）。
    expect(screen.getByText('审批已失效')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '拒绝' })).not.toBeInTheDocument()
    expect(screen.queryByText(/该审批已超时失效，未执行任何操作/)).not.toBeInTheDocument()
  })

  it('expands a finished decision back to the full card on demand', async () => {
    const user = userEvent.setup()
    render(<ApprovalCard approval={approvalOf(approvalExpiredScenario)} />)

    await user.click(screen.getByRole('button', { name: '展开详情' }))

    expect(screen.getByText(/该审批已超时失效，未执行任何操作/)).toBeInTheDocument()
    // 展开态能收回，不会把自己锁死。
    await user.click(screen.getByRole('button', { name: '收起' }))
    expect(screen.queryByText(/该审批已超时失效，未执行任何操作/)).not.toBeInTheDocument()
  })

  it('lets the user dismiss a finished decision entirely', async () => {
    const user = userEvent.setup()
    const { container } = render(<ApprovalCard approval={approvalOf(approvalExpiredScenario)} />)

    await user.click(screen.getByRole('button', { name: '关闭审批结果' }))

    expect(container).toBeEmptyDOMElement()
  })

  it('keeps a dismissed decision dismissed after a remount (refresh)', () => {
    // 终态卡片由服务端快照派生，刷新后会重新挂载。只用组件状态记"关过了"的话，
    // 刷新等于白关——用户实测反馈的"关不掉"就是这个（2026-09-30）。
    const expired = approvalOf(approvalExpiredScenario)
    localStorage.setItem('local-agent-web.dismissed-approvals', JSON.stringify([expired.approvalId]))

    const { container } = render(<ApprovalCard approval={expired} />)

    expect(container).toBeEmptyDOMElement()
  })

  it('still shows a different approval after another one was dismissed', () => {
    const base = approvalOf(approvalScenario)
    localStorage.setItem('local-agent-web.dismissed-approvals', JSON.stringify(['某个别的审批']))

    render(<ApprovalCard approval={base} />)

    expect(screen.getByText('等待审批')).toBeInTheDocument()
  })

  it('survives unreadable storage instead of failing to render', () => {
    // 隐私模式 / 配额满：读不到就退回"没关过"，而不是把卡片渲染搞崩。
    localStorage.setItem('local-agent-web.dismissed-approvals', '{ 不是 JSON')

    render(<ApprovalCard approval={approvalOf(approvalExpiredScenario)} />)

    expect(screen.getByText('审批已失效')).toBeInTheDocument()
  })

  it('never offers collapse or dismiss while a decision is still pending', () => {
    // 待决卡片是页面上唯一需要用户动作的东西：收起或关掉它，会让人以为"不做决定也行"。
    render(<ApprovalCard approval={approvalOf(approvalScenario)} />)

    expect(screen.queryByRole('button', { name: '展开详情' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '收起' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '关闭审批结果' })).not.toBeInTheDocument()
  })

  it('reports a taken decision instead of asking again', () => {
    const base = approvalOf(approvalScenario)
    const label = { executing: '执行中', approved: '已批准', rejected: '已拒绝' } as const
    for (const state of ['executing', 'approved', 'rejected'] as const) {
      const { unmount } = render(<ApprovalCard approval={{ ...base, state, note: null }} />)
      expect(screen.queryByRole('button', { name: '允许一次' })).not.toBeInTheDocument()
      // 收起态只报告结果，而"结果"正是这一行要说的东西。
      expect(screen.getByText(label[state])).toBeInTheDocument()
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
      expect(screen.getByText('路径：dist')).toBeInTheDocument()
      expect(screen.getByText('包含子目录：True')).toBeInTheDocument()
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
