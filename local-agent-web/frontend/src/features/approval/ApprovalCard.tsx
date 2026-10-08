import { useState } from 'react'
import { WarningTriangle } from '../../components/icons'
import type { ApprovalView } from '../../fixtures/types'
import { parameterLabel, toolLabel } from '../tool/display'
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

/**
 * 被用户关掉的审批结果（按 `approvalId` 记）。
 *
 * 存 localStorage 而**不是组件状态**：终态卡片由服务端快照派生（`project_approval` 会从
 * `approval_history` 的最后一条决定重新算出来），所以只用 `useState` 记的话，用户关掉它、
 * 一刷新它又回来了——看起来就是"关不掉"（用户实测反馈，2026-09-30）。
 *
 * 这是**界面偏好**，不是账本：它不改变任何决定、不影响服务端，也不需要同步。读不到就退回
 * "没关过"（隐私模式、存储配额）——宁可让它重新出现，也不要因为存储不可用而崩掉渲染。
 */
const DISMISSED_KEY = 'local-agent-web.dismissed-approvals'

/** 只留最近这些条：界面偏好不该无限长大。 */
const DISMISSED_KEEP = 50

function readDismissed(): ReadonlySet<string> {
  try {
    const raw = globalThis.localStorage?.getItem(DISMISSED_KEY)
    if (raw === null || raw === undefined) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((item): item is string => typeof item === 'string'))
  } catch {
    return new Set()
  }
}

function writeDismissed(ids: ReadonlySet<string>): void {
  try {
    globalThis.localStorage?.setItem(DISMISSED_KEY, JSON.stringify([...ids].slice(-DISMISSED_KEEP)))
  } catch {
    // 存储不可用：这次会话内仍然关得掉，只是刷新之后会回来。
  }
}

const STATE_LABEL: Record<ApprovalView['state'], string> = {
  pending: '等待审批',
  executing: '执行中',
  approved: '已批准',
  rejected: '已拒绝',
  expired: '审批已失效',
  interrupted: '执行已中断 · 结果需核对',
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
  const [dismissedIds, setDismissedIds] = useState<ReadonlySet<string>>(readDismissed)
  const dismissed = dismissedIds.has(approval.approvalId)
  const dismiss = () => {
    const next = new Set(dismissedIds)
    next.add(approval.approvalId)
    setDismissedIds(next)
    writeDismissed(next)
  }
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
          <span className={styles.action}>{toolLabel(approval.action)}</span>
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
            onClick={dismiss}
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
        <span className={styles.action}>{toolLabel(approval.action)}</span>
        {approval.risk === 'high' ? <span className={styles.risk}>高风险</span> : null}
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
            onClick={dismiss}
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
              <details>
              <summary>查看操作参数</summary>
              <ul className={`${styles.impact} ${styles.parameters}`}>
                {parameters.map((parameter) => (
                  <li key={parameter.name}>
                    {parameterLabel(parameter.name)}：{parameter.value}
                  </li>
                ))}
              </ul>
              </details>
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
