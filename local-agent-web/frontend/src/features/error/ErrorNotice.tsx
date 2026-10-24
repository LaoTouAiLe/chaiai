import { useState } from 'react'
import { ChevronRight, WarningTriangle } from '../../components/icons'
import type { ErrorNoticeView } from '../../fixtures/types'
import styles from './ErrorNotice.module.css'

interface ErrorNoticeProps {
  notice: ErrorNoticeView
  dismissed?: boolean
  onToggleDismissed?: () => void
}

const CATEGORY_LABEL: Record<ErrorNoticeView['category'], string> = {
  configuration: '配置',
  model: '模型',
  tool: '工具',
  security: '安全',
  approval: '审批',
  recovery: '恢复',
  system: '系统',
}

/**
 * Provider failure notice — **early visual checkpoint 2** of T26.
 *
 * Contract this prototype must demonstrate:
 * - a severity treatment that is visibly different from normal output;
 * - an actionable hint, always present (never "something went wrong");
 * - a stable code plus a safe correlation id behind an explicit details control, and **no** raw provider payload,
 *   key material or absolute private path;
 * - retry guidance without an action that could repeat a tool side effect.
 *
 * 2026-10-16 收窄：**默认收起成一行**。原来标题、原因、下一步、重试口径、诊断信息一路铺开，
 * 一屏里两条就把对话顶掉半屏（用户实拍原话："不用这么大红弹窗显示"）。现在只有一行标题，
 * 点开才是那些内容——契约要求的四项（可读原因、可照做的下一步、稳定 code、重试口径）
 * **一条都没少**，只是不再抢注意力。
 */
export function ErrorNotice({ notice, dismissed, onToggleDismissed }: ErrorNoticeProps) {
  const [dismissedId, setDismissedId] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const hidden = dismissed ?? dismissedId === notice.diagnosticId
  const toggle = () => {
    if (onToggleDismissed !== undefined) onToggleDismissed()
    else setDismissedId(hidden ? null : notice.diagnosticId)
  }
  if (hidden) return <button type="button" className={styles.dismissed}
    onClick={toggle}>查看已关闭的错误：{notice.title}</button>
  const severityClass = notice.severity === 'warning' ? styles.warning : styles.error
  return (
    <section className={`${styles.notice} ${severityClass}`} aria-label={`${CATEGORY_LABEL[notice.category]}错误`}>
      <div className={styles.head}>
        <button type="button" className={styles.toggle} aria-expanded={open}
          onClick={() => setOpen(current => !current)}>
          <span className={styles.icon} aria-hidden="true">
            <WarningTriangle />
          </span>
          <span className={styles.title}>{notice.title}</span>
          <span className={styles.chevron} data-open={open} aria-hidden="true">
            <ChevronRight />
          </span>
        </button>
        <button type="button" className={styles.close} aria-label={`关闭错误提示：${notice.title}`}
          onClick={toggle}>×</button>
      </div>
      {open ? (
        <>
          <p className={styles.message}>{notice.message}</p>
          <p className={styles.hint}>{notice.hint}</p>
          <div className={styles.foot}>
            {notice.retryable ? (
              <span className={styles.notRetryable}>检查后可在输入框重新发送</span>
            ) : (
              // Per-notice, not per-component: a busy session and a broken key block a
              // retry for different reasons (plan §6.4).
              <span className={styles.notRetryable}>{notice.blockedReason ?? '不能自动重试'}</span>
            )}
          </div>
          <details className={styles.diagnostics}>
            <summary>诊断信息</summary>
            <span className={styles.meta}>{notice.code} · {notice.diagnosticId}</span>
          </details>
        </>
      ) : null}
    </section>
  )
}
