import type { ReactNode } from 'react'

import styles from './primitives.module.css'

export type BadgeTone = 'neutral' | 'success' | 'warn' | 'danger' | 'accent'

const TONE_CLASS: Record<BadgeTone, string> = {
  neutral: styles.toneNeutral,
  success: styles.toneSuccess,
  warn: styles.toneWarn,
  danger: styles.toneDanger,
  accent: styles.toneAccent,
}

/**
 * 唯一的状态标签实现（§7.2）。
 *
 * 状态一律用 Badge，**不要**再用裸彩色文字：仓库里目前有 5 种写法
 * （「可用」「当前档位没有」「默认」「已验证」以及红字「删除」），
 * 视觉重量各不相同，拼在一起就显糙。
 */
export function Badge({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: BadgeTone
  children: ReactNode
  title?: string
}) {
  return (
    <span className={`${styles.badge} ${TONE_CLASS[tone]}`} title={title}>
      {children}
    </span>
  )
}
