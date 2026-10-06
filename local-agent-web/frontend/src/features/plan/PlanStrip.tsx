import { useState } from 'react'
import { Check, ChevronRight, Cross } from '../../components/icons'
import type { PlanView } from '../../fixtures/types'
import styles from './PlanStrip.module.css'

interface PlanStripProps {
  plan: PlanView
}

const STATUS_CLASS: Record<PlanView['steps'][number]['status'], string> = {
  pending: styles.pending ?? '',
  running: styles.running ?? '',
  completed: styles.completed ?? '',
  failed: styles.failed ?? '',
  skipped: styles.pending ?? '',
  blocked: styles.failed ?? '',
}

const STATUS_LABEL: Record<PlanView['steps'][number]['status'], string> = {
  pending: '待处理',
  running: '进行中',
  completed: '已完成',
  failed: '失败',
  skipped: '已跳过',
  blocked: '受阻',
}

/**
 * Compact plan strip that sits above the composer.
 *
 * `completed` steps and `failed` steps are visually distinct because "完成一步"
 * never means "完成整个任务" (ARCHITECTURE §15.2 / T20 semantics).
 *
 * **默认收起**，点击标题栏展开。两条实测理由（2026-09-30）：
 *
 * - 计划常常五六条、每条都是一整句，展开着会把输入区上方占满；
 * - 长步骤会横向撑破卡片（见 `module.css` 里 `min-width: 0` 的说明）。
 *
 * 展开态是**界面状态**：刷新后回到收起，与服务端无关（计划本身来自快照，不会因此丢）。
 * 标题栏那个箭头一直暗示"可展开"，这里把它兑现。
 */
export function PlanStrip({ plan }: PlanStripProps) {
  const [expanded, setExpanded] = useState(false)
  const done = plan.steps.filter((step) => step.status === 'completed').length
  const running = plan.steps.find((step) => step.status === 'running')

  return (
    <section className={styles.strip} aria-label="当前计划">
      <button
        type="button"
        className={styles.head}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className={styles.goal}>{plan.goal}</span>
        <span className={styles.count}>
          {done}/{plan.steps.length} 已完成
        </span>
        {running === undefined ? null : <span className={styles.current}>· {running.title}</span>}
        <span className={expanded ? `${styles.expand} ${styles.expandOpen}` : styles.expand} aria-hidden="true">
          <ChevronRight />
        </span>
      </button>
      {expanded ? (
        <ol className={styles.steps}>
          {plan.steps.map((step) => (
            <li key={step.id} className={`${styles.step} ${STATUS_CLASS[step.status]}`}>
              <span className={styles.stepIcon} aria-hidden="true">
                {step.status === 'completed' ? <Check /> : step.status === 'failed' ? <Cross /> : null}
              </span>
              <span className={styles.stepTitle} title={step.title}>{step.title}</span>
              <span className={styles.stepStatus}>{STATUS_LABEL[step.status]}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  )
}
