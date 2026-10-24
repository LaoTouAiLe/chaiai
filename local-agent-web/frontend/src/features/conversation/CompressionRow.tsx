import type { CompressionRowView } from '../../fixtures/types'
import styles from './CompressionRow.module.css'

/**
 * 上下文压缩在**对话流里**的一行（2026-10-16）。
 *
 * 为什么不放在输入框那圈 chrome 上：它不是"输入状态"，而是流程里发生过的一步——和 `Think`、
 * 工具执行是同一层级的东西，读者按时间顺序读下来才能理解"为什么这轮的上下文突然变小了"。
 *
 * 两条如实口径：
 * - **运行中**只说"正在压缩"（那时还没有条数与前后 token，写 0 就是编数）；
 * - 已完成的说清**折叠了多少条**与**估算**输入的前后变化（估算值不是供应商实际量，所以不写"省了多少 token"）。
 */
export function CompressionRow({ compression }: { compression: CompressionRowView }) {
  return (
    <div className={styles.root} data-state={compression.running ? 'running' : 'complete'}>
      <span className={styles.icon} aria-hidden="true">
        ❒
      </span>
      <span className={styles.title}>上下文压缩</span>
      <span className={styles.detail} role="status">
        {compression.running ? '正在压缩…' : detailText(compression)}
      </span>
    </div>
  )
}

function tokenLabel(value: number | null): string {
  if (value === null) return '未知'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`
  return String(value)
}

function detailText(compression: CompressionRowView): string {
  const folded = compression.foldedMessages
  // 单位只写一次（踩过：`折叠 151 条 条消息`——拼接处各写了一次"条"）。
  const head = folded === null ? '折叠条数未知' : `折叠 ${folded} 条消息`
  if (compression.tokensBefore === null || compression.tokensAfter === null) return head
  return `${head} · 估算输入 ${tokenLabel(compression.tokensBefore)} → ${tokenLabel(compression.tokensAfter)}`
}
