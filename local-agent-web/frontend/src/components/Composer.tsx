import type {
  ComposerStateView,
  ComposerTakeoverView,
  ContextMeterView,
  ModelMenuView,
  PlanView,
  SessionSummaryView,
} from '../fixtures/types'
import { ApprovalCard } from '../features/approval/ApprovalCard'
import type { ApprovalActions } from '../features/approval/ApprovalCard'
import { PlanStrip } from '../features/plan/PlanStrip'
import { ModelMenu } from '../features/settings/ModelMenu'
import { ContextMeter, MeterPanel } from './ContextMeter'
import styles from './Composer.module.css'

/**
 * Real wiring (T30). **Omitting it means prototype behaviour**: the input stays
 * uncontrolled and the send button stays permanently disabled, which is exactly
 * what the static baseline shows.
 */
export interface ComposerActions {
  /** Controlled draft. */
  draft: string
  onDraftChange: (text: string) => void
  onSend: () => void
  /** Whether sending is possible right now: non-empty draft and no turn in flight. */
  canSend: boolean
  /** Real shell only: the model chip opens or closes the session selector. */
  onToggleModelMenu?: () => void
}

interface ComposerProps {
  session: SessionSummaryView
  plan: PlanView | null
  meter: ContextMeterView
  takeover: ComposerTakeoverView
  modelMenu: ModelMenuView | null
  /** Disabled/phase overrides; omitted means enabled with no running phase. */
  state?: ComposerStateView
  actions?: ComposerActions
  modelMenuActions?: React.ComponentProps<typeof ModelMenu>['actions']
  /**
   * Approval decision wiring (T32); omitted in prototype mode so the static card
   * keeps its handler-free buttons.
   */
  approvalActions?: ApprovalActions
}

/** One copy of the running-state explanation, derived from `session.status`. */
const SEND_BLOCKED_TEXT = '任务进行中：可以先继续写草稿，但要等这一轮结束才能发送。'

/**
 * Resident input area: plan strip on top, then either the composer or whatever
 * request has taken it over.
 *
 * An approval replaces the composer **only while a decision is still required**:
 * the chat input must not stay reachable underneath a pending decision
 * (`ARCHITECTURE.md` §15.8). Once the request is no longer actionable — it
 * expired, or a decision was already taken — the composer comes back and the
 * card stays above it as a visible outcome, because a card that asks the user to
 * re-issue a request must not be the reason they cannot type one.
 */
export function Composer({ session, plan, meter, takeover, modelMenu, state, actions, modelMenuActions, approvalActions }: ComposerProps) {
  const approval = takeover.kind === 'approval' ? takeover.approval : null
  const awaitingDecision = approval !== null && approval.state === 'pending'
  const disabledReason = state?.disabledReason ?? null
  const phase = state?.phase ?? null
  const meterOpen = state?.meterOpen ?? false
  // A running turn blocks *sending*, not writing: the draft stays editable, the
  // input is never replaced by a stop button, and the reason is derived from the
  // run state itself rather than repeated per fixture (plan §T30, ARCHITECTURE §15.2).
  const sendBlockedReason = session.status === 'running' ? SEND_BLOCKED_TEXT : null

  return (
    <div className={styles.dock}>
      <div className={styles.stack}>
        {plan === null ? null : <PlanStrip plan={plan} />}

        {approval !== null && !awaitingDecision ? (
          <ApprovalCard approval={approval} actions={approvalActions} />
        ) : null}

        {modelMenu === null || awaitingDecision ? null : <ModelMenu menu={modelMenu} actions={modelMenuActions} />}

        {meterOpen && !awaitingDecision ? <MeterPanel meter={meter} /> : null}

        {awaitingDecision ? (
          <ApprovalCard approval={approval} actions={approvalActions} />
        ) : (
          <>
            {phase === null ? null : (
              <div className={styles.phase} role="status">
                {phase}
              </div>
            )}
            <div className={styles.composer}>
              <label className={styles.inputLabel}>
                <span className="visually-hidden">给智能体发消息</span>
                {/* A disabled input states why it is disabled, and points at that
                    sentence for assistive tech rather than relying on the greying. */}
                <textarea
                  className={styles.input}
                  placeholder="给智能体发消息"
                  rows={1}
                  disabled={disabledReason !== null}
                  aria-describedby={disabledReason === null ? undefined : 'composer-disabled-reason'}
                  // Prototype mode stays uncontrolled (untouched); real mode is driven by the draft,
                  // which lives in the UI store so a turn in flight never eats what the user typed.
                  value={actions === undefined ? undefined : actions.draft}
                  onChange={
                    actions === undefined
                      ? undefined
                      : (event) => {
                        actions.onDraftChange(event.target.value)
                      }
                  }
                  onKeyDown={
                    actions === undefined
                      ? undefined
                      : (event) => {
                        // Enter sends, Shift+Enter keeps the newline (chat convention).
                        if (event.key === 'Enter' && !event.shiftKey && actions.canSend) {
                          event.preventDefault()
                          actions.onSend()
                        }
                      }
                  }
                />
              </label>
              {disabledReason === null ? null : (
                <p className={styles.disabledNote} id="composer-disabled-reason">
                  {disabledReason}
                </p>
              )}
              {sendBlockedReason === null ? null : (
                <p className={styles.disabledNote} id="composer-send-blocked">
                  {sendBlockedReason}
                </p>
              )}
              <div className={styles.controls}>
                <button type="button" className={styles.command} aria-label="命令">
                  <span aria-hidden="true">＋</span>
                </button>
                <button type="button" className={styles.chip}>
                  Workspace Write
                </button>
                <button
                  type="button"
                  className={styles.chip}
                  onClick={actions?.onToggleModelMenu}
                  aria-expanded={actions?.onToggleModelMenu === undefined ? undefined : modelMenu !== null}
                  aria-label={actions?.onToggleModelMenu === undefined ? undefined : '选择会话模型'}
                >
                  {actions !== undefined && session.modelLabel === '' ? '选择模型' : session.modelLabel}
                </button>
                <div className={styles.spacer} />
                <ContextMeter meter={meter} open={meterOpen} />
                {/* v0.4 exposes no `cancel_task()`, so the primary action is never a
                    stop button: sending is simply unavailable until the turn ends
                    (ARCHITECTURE §15.2, plan §T30). */}
                <button
                  type="button"
                  className={styles.send}
                  // Prototype mode keeps it permanently disabled (the fixture snapshot);
                  // real mode enables it once there is something to send.
                  disabled={actions === undefined || !actions.canSend}
                  onClick={actions === undefined ? undefined : () => actions.onSend()}
                  aria-label="发送消息"
                  aria-describedby={sendBlockedReason === null ? undefined : 'composer-send-blocked'}
                >
                  <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
                    <path
                      d="M7 11.5V3M7 3 3.4 6.6M7 3l3.6 3.6"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.7"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
