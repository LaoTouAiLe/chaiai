import { useState } from 'react'
import { WarningTriangle } from '../../components/icons'
import type { ApprovalView } from '../../fixtures/types'
import styles from './ApprovalCard.module.css'

/**
 * Real wiring (T32). **Omitting it means prototype behaviour**: the buttons render
 * with no handler and no `busy` state, which is what the static baseline shows.
 */
export interface ApprovalActions {
  onApprove: (confirmRecursive: boolean) => void
  onReject: () => void
  /** A decision is in flight: both buttons lock until the server answers. */
  busy: boolean
}

interface ApprovalCardProps {
  approval: ApprovalView
  actions?: ApprovalActions
}

const STATE_LABEL: Record<ApprovalView['state'], string> = {
  pending: '等待审批',
  executing: '执行中',
  approved: '已批准',
  rejected: '已拒绝',
  expired: '审批已失效',
}

/**
 * The card that takes over the composer while a side effect needs a decision.
 *
 * Four rules this screen has to make visible:
 * - the action, its normalised target, the parameters it will run with, its
 *   impact and its risk are all shown before any button is offered;
 * - a decision is one-shot: once taken (or once stale) the buttons disappear and
 *   the card only reports the outcome;
 * - the buttons lock the moment they are used, and the **server's answer is the
 *   authority** — a locked button that then re-enables would be claiming the
 *   decision did not happen;
 * - the front-end button is never the authority that executes anything
 *   (`ARCHITECTURE.md` §15.8).
 */
export function ApprovalCard({ approval, actions }: ApprovalCardProps) {
  const [confirmedFor, setConfirmedFor] = useState<string | null>(null)
  /**
   * 终态（已批准 / 已拒绝 / 已失效）**默认收起成一行**，用户可展开回看，也可整条关掉。
   *
   * 结果该留在屏幕上——"我确实拒绝过"是有用的记录，刷新之后也仍然在（它由服务端快照派生）。
   * 但它住在**接管位**：一直展开就等于永久霸占输入区上方，用户实测反馈"页面非常不友好"。
   *
   * 收起是**界面状态**，不回写服务端：它不改变任何决定，也不影响刷新恢复。
   * `null` = 用户还没表态过，此时跟随状态——待决必展开（要你做决定），终态必收起。
   */
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null)
  const [dismissed, setDismissed] = useState(false)
  const actionable = approval.state === 'pending'
  const expanded = expandedOverride ?? actionable
  const secondStage = approval.risk === 'high' && approval.confirmationStage === 2
  const confirmationKey = `${approval.approvalId}:${String(approval.confirmationStage)}:${approval.target}`
  const confirmed = confirmedFor === confirmationKey
  const tone = approval.risk === 'high' ? styles.high : styles.normal
  // Only lock in real mode: prototype mode passes no actions, so the attribute is
  // never rendered there and the static baseline cannot drift.
  const busy = actions?.busy === true
  const parameters = approval.parameters ?? []

  if (dismissed) return null

  if (!expanded) {
    return (
      <section className={`${styles.card} ${styles.collapsed} ${tone}`} aria-label="审批">
        <div className={styles.head}>
          <span className={styles.icon} aria-hidden="true">
            <WarningTriangle />
          </span>
          <span className={styles.state}>{STATE_LABEL[approval.state]}</span>
          <span className={styles.action}>{approval.action}</span>
          {approval.target === '' ? null : (
            <span className={styles.collapsedTarget}>{approval.target}</span>
          )}
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={false}
            onClick={() => setExpandedOverride(true)}
          >
            展开详情
          </button>
          <button
            type="button"
            className={styles.dismiss}
            aria-label="关闭审批结果"
            onClick={() => setDismissed(true)}
          >
            ×
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className={`${styles.card} ${tone}`} aria-label="审批">
      <div className={styles.head}>
        <span className={styles.icon} aria-hidden="true">
          <WarningTriangle />
        </span>
        <span className={styles.state}>{STATE_LABEL[approval.state]}</span>
        <span className={styles.action}>{approval.action}</span>
        {approval.risk === 'high' ? <span className={styles.risk}>高风险</span> : null}
        <span className={styles.id}>{approval.approvalId}</span>
        {actionable ? null : (
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={true}
            onClick={() => setExpandedOverride(false)}
          >
            收起
          </button>
        )}
        {actionable ? null : (
          <button
            type="button"
            className={styles.dismiss}
            aria-label="关闭审批结果"
            onClick={() => setDismissed(true)}
          >
            ×
          </button>
        )}
      </div>

      <p className={styles.summary}>{approval.summary}</p>
      {approval.outsideWorkspace ? <p className={styles.summary}>工作区外路径</p> : null}
      {approval.risk === 'high' ? (
        <p className={styles.summary}>
          {secondStage ? '第二次确认：请再次核对目标和影响范围。' : '第一次确认只会进入二次确认，不会执行删除。'}
        </p>
      ) : null}

      <dl className={styles.facts}>
        {approval.target === '' ? null : (
          <div className={styles.fact}>
            <dt>目标</dt>
            <dd className={styles.mono}>{approval.target}</dd>
          </div>
        )}
        {parameters.length === 0 ? null : (
          <div className={styles.fact}>
            <dt>参数</dt>
            <dd className={styles.mono}>
              <ul className={styles.impact}>
                {parameters.map((parameter) => (
                  <li key={parameter.name}>
                    {parameter.name}={parameter.value}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
        {approval.impact.length === 0 ? null : (
          <div className={styles.fact}>
            <dt>影响</dt>
            <dd>
              <ul className={styles.impact}>
                {approval.impact.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>

      {actionable ? (
        <div className={styles.actions}>
          {secondStage ? (
            <label className={styles.hint}>
              <input type="checkbox" checked={confirmed} onChange={(event) => setConfirmedFor(event.target.checked ? confirmationKey : null)} />
              我确认删除该目录及其全部内容
            </label>
          ) : null}
          <span className={styles.hint}>
            {busy ? '正在提交，结果以后端为准…' : '批准只对本次调用生效，不会加入免审名单。'}
          </span>
          <button
            type="button"
            className={styles.reject}
            onClick={actions === undefined ? undefined : () => actions.onReject()}
            disabled={actions === undefined ? undefined : busy}
          >
            拒绝
          </button>
          <button
            type="button"
            className={styles.allow}
            onClick={actions === undefined ? undefined : () => actions.onApprove(secondStage && confirmed)}
            disabled={actions === undefined ? undefined : busy || (secondStage && !confirmed)}
          >
            {secondStage ? '确认删除' : approval.risk === 'high' ? '进入二次确认' : '允许一次'}
          </button>
        </div>
      ) : (
        <p className={styles.outcome}>{approval.note ?? `当前状态：${STATE_LABEL[approval.state]}`}</p>
      )}
    </section>
  )
}
