import { WarningTriangle } from '../../components/icons'
import type { ErrorNoticeView } from '../../fixtures/types'
import styles from './ErrorNotice.module.css'

interface ErrorNoticeProps {
  notice: ErrorNoticeView
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
 * - a stable code plus a safe correlation id, and **no** raw provider payload,
 *   key material or absolute private path;
 * - retry offered only when the failure is actually retryable.
 */
export function ErrorNotice({ notice }: ErrorNoticeProps) {
  const severityClass = notice.severity === 'warning' ? styles.warning : styles.error
  return (
    <section className={`${styles.notice} ${severityClass}`} aria-label={`${CATEGORY_LABEL[notice.category]}错误`}>
      <div className={styles.head}>
        <span className={styles.icon} aria-hidden="true">
          <WarningTriangle />
        </span>
        <h2 className={styles.title}>{notice.title}</h2>
        <span className={styles.code}>{notice.code}</span>
      </div>
      <p className={styles.message}>{notice.message}</p>
      <p className={styles.hint}>{notice.hint}</p>
      <div className={styles.foot}>
        <span className={styles.meta}>
          {CATEGORY_LABEL[notice.category]} · 诊断编号 {notice.diagnosticId}
        </span>
        {notice.retryable ? (
          <button type="button" className={styles.retry}>
            重新发送
          </button>
        ) : (
          // Per-notice, not per-component: a busy session and a broken key block a
          // retry for different reasons (plan §6.4).
          <span className={styles.notRetryable}>{notice.blockedReason ?? '不能自动重试'}</span>
        )}
      </div>
    </section>
  )
}
