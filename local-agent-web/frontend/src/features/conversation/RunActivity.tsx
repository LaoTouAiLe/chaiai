import { useEffect, useState } from 'react'
import type { RunActivityView } from '../../fixtures/types'
import styles from './RunActivity.module.css'

/** A display clock; neither polling nor model requests are needed to animate it. */
export function RunActivity({ activity }: { activity: RunActivityView }) {
  const [now, setNow] = useState(Date.now)
  useEffect(() => {
    if (!activity.active || activity.startedAt === null) return
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [activity.active, activity.startedAt])

  const start = activity.startedAt === null ? NaN : Date.parse(activity.startedAt)
  const seconds = Math.max(0, Math.floor((now - start) / 1_000))
  const elapsed = seconds < 60 ? `${seconds} 秒` : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
  return (
    <section className={styles.activity} data-active={activity.active} aria-label="当前运行状态">
      <span className={styles.indicator} aria-hidden="true" />
      <div className={styles.content}>
        <div className={styles.head}>
          <span className={styles.label} role="status">{activity.label}</span>
          {activity.active && Number.isFinite(start) && start > 0 ? (
            <span className={styles.elapsed} aria-hidden="true">本轮已用 {elapsed}</span>
          ) : null}
        </div>
        <p className={styles.detail}>{activity.detail}</p>
      </div>
    </section>
  )
}
