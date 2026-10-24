// 「任务中心」面板（v0.6 T62；契约 §17.6）。
//
// 这个面板要回答三件事：**有哪些任务**、**它们到哪一步了**、**现在能做什么**。三条硬规矩：
//   1. **只读**——页面不是授权真源；这里没有"改任务状态"的入口，状态只由提交 / 取消 / 重启收口产生；
//   2. **不出现内部标识**——不显示任务 id、轮次 id、工具名或指纹（§15.8），只显示状态、时长与原因；
//   3. **"在等什么"由状态推导**（闭集，见 `adapters.ts`）——不读会话内部状态，因此不会与真源不一致。
//
// 展示组件不认识 fetch：数据由 `TasksSection` 取好、映射成视图模型后传进来（与 `CapabilitiesPanel`
// 同一分工）。

import { useEffect, useState } from 'react'

import type { TaskState, TasksView } from '../../fixtures/types'
import {
  TASK_FILTERS,
  TASK_FILTER_LABEL,
  taskFilterCounts,
  taskRows,
  type TaskFilter,
} from './adapters'
import styles from './TasksPanel.module.css'

interface TasksPanelProps {
  /** `null` = 还没读到（读失败也走这里，错误单独挂在 `errorText` 上）。 */
  view: TasksView | null
  /** 最近一次读取失败的用户可读文本；没有就不显示。 */
  errorText?: string | null
  /** 打开某个会话（恢复入口：三类判定的文案都指向"去会话里处理"）。 */
  onOpenSession: (sessionId: string) => void
  /** 人工核对结案（§17.1 唯一例外）：不传就不渲染结案按钮（原型与测试保持纯展示）。 */
  onCloseReview?: (taskId: string, outcome: 'completed' | 'cancelled') => void
  /** 关闭面板；不传就是纯展示（测试与原型用）。 */
  onClose?: () => void
}

/** 打开会话的按钮文案：按状态说清楚"去那里做什么"。 */
const OPEN_LABEL: Record<TaskState, string> = {
  queued: '打开会话',
  running: '打开会话',
  waiting_user: '打开会话做决定',
  completed: '查看结果',
  failed: '查看结果',
  cancelled: '查看结果',
  needs_review: '打开会话核对',
}

/**
 * 只在会让用户误解的行上补一句。**§17.6 逐项要求**，不是这里的自由发挥：
 *
 * - **在跑**（`running`）与已取消（`cancelled`）：必须写明取消**不撤销**已经发生的副作用——
 *   否则用户会以为取消等于"文件回到原样"（在跑那一行尤其重要：那里正是用户要考虑取消的地方）；
 * - `needs_review`：不自动重跑——不写这句，用户会以为停一会儿它自己会继续。
 */
const ROW_NOTE: Partial<Record<TaskState, string>> = {
  running: '取消不撤销已经发生的副作用。',
  cancelled: '取消不撤销已经发生的副作用。',
  needs_review: '不自动重跑：核对后重新提交就是新任务。',
}

export function TasksPanel({ view, errorText = null, onOpenSession, onClose, onCloseReview }: TasksPanelProps) {
  const [filter, setFilter] = useState<TaskFilter>('all')
  const [now, setNow] = useState(Date.now)
  const counts = view === null ? null : taskFilterCounts(view)
  const rows = view === null ? [] : taskRows(view, filter, now)
  // 有任务在跑就每秒重算一次时长（纯展示时钟，不轮询服务端、不动索引——与 `RunActivity` 同一做法）。
  const live = rows.some((row) => row.ticking)
  useEffect(() => {
    if (!live) return
    const timer = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [live])

  return (
    <div className={styles.overlay} role="presentation">
      <section className={styles.dialog} role="dialog" aria-modal="true" aria-label="任务中心">
        <header className={styles.head}>
          <div>
            <h2 className={styles.title}>任务中心</h2>
            <p className={styles.note}>
              当前工作区的任务与它们到哪一步了。"在等什么"由任务状态决定，这一页不读会话内部状态，
              也没有改任务的入口——任务状态只由提交、取消和重启收口产生。
            </p>
          </div>
          {onClose === undefined ? null : (
            <button type="button" className={styles.close} onClick={onClose}>
              关闭
            </button>
          )}
        </header>

        {errorText === null ? null : (
          <p className={styles.error} role="alert">
            {errorText}
          </p>
        )}

        {view === null ? (
          <p className={styles.detail}>还没有读到任务——读到之前不显示任何一项。</p>
        ) : (
          <>
            <div className={styles.filters} role="group" aria-label="按状态筛选">
              {TASK_FILTERS.map((item) => (
                <button
                  key={item}
                  type="button"
                  className={filter === item ? `${styles.filter} ${styles.filterOn}` : styles.filter}
                  aria-pressed={filter === item}
                  onClick={() => setFilter(item)}
                >
                  {TASK_FILTER_LABEL[item]}
                  <span className={styles.filterCount}>{counts?.[item] ?? 0}</span>
                </button>
              ))}
            </div>

            {rows.length === 0 ? (
              <p className={styles.detail}>
                {view.tasks.length === 0
                  ? '这个工作区还没有任务。发起一次提问之后，它会出现在这里。'
                  : '当前筛选下没有任务。'}
              </p>
            ) : (
              <ul className={styles.list}>
                {rows.map((row) => (
                  <li key={row.taskId} className={styles.row} data-state={row.state}>
                    <div className={styles.rowHead}>
                      <span className={styles.state}>{row.stateLabel}</span>
                      <span className={styles.session}>{row.sessionTitle}</span>
                    </div>
                    <p className={styles.meta}>
                      {row.durationText === null ? '未记录起始时间' : `已运行 ${row.durationText}`}
                      {/* 分隔符与文本分开放：每个 span 的文本**正好**是那句话，
                          断言才能用 exact 匹配（默认的子串匹配会撞上别处出现的同一个词）。 */}
                      <span className={styles.waiting} aria-hidden="true">·</span>
                      <span className={styles.waiting}>{row.waitingLabel}</span>
                      {row.finishedText === null ? null : (
                        <>
                          <span className={styles.waiting} aria-hidden="true">·</span>
                          <span className={styles.waiting}>{row.finishedText}结束</span>
                        </>
                      )}
                    </p>
                    {row.detail === null ? null : <p className={styles.detail}>{row.detail}</p>}
                    {ROW_NOTE[row.state] === undefined ? null : (
                      <p className={styles.rowNote}>{ROW_NOTE[row.state]}</p>
                    )}
                    <button
                      type="button"
                      className={styles.open}
                      onClick={() => onOpenSession(row.sessionId)}
                    >
                      {OPEN_LABEL[row.state]}
                    </button>
                    {/* 人工结案（§17.1 唯一例外，2026-10-23）：`needs_review` 是终态，
                        以前没有任何出口，任务会把会话永远钉在侧栏的「需人工核对」上。 */}
                    {row.state !== 'needs_review' || onCloseReview === undefined ? null : (
                      <>
                        <button type="button" className={styles.open}
                          onClick={() => onCloseReview(row.taskId, 'completed')}>
                          我核对过，已完成
                        </button>
                        <button type="button" className={styles.open}
                          onClick={() => onCloseReview(row.taskId, 'cancelled')}>
                          我核对过，未完成
                        </button>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </div>
  )
}
