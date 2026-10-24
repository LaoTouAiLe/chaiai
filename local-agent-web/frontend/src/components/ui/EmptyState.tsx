import type { ReactNode } from 'react'

import styles from './primitives.module.css'

/**
 * 唯一的空态实现（§5.9）。
 *
 * `compact` 用于面板内（右栏标签、设置分区），普通版用于整页。
 * 空态必须说明"这里会出现什么"，不要只写「暂无数据」。
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  compact = false,
}: {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
  compact?: boolean
}) {
  return (
    <div className={compact ? `${styles.empty} ${styles.emptyCompact}` : styles.empty}>
      {icon === undefined ? null : <div className={styles.emptyIcon}>{icon}</div>}
      <p className={styles.emptyTitle}>{title}</p>
      {description === undefined ? null : <p className={styles.emptyDescription}>{description}</p>}
      {action === undefined ? null : <div className={styles.emptyAction}>{action}</div>}
    </div>
  )
}
