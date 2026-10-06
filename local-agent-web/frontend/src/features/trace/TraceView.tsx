// 轨迹视图（matrix 20 表格态 + 21 检查器）：一个入口，两段信息。
//
// 左侧是工具调用的时间线表格（类型 / 参数 / 结果摘要），点任一行在右侧展开检查器。参考把 20 与 21
// 分成"页签"和"工具卡上的 Inspect"两个入口；本项目 v0.4 用一个入口承载同一份数据，避免两处各说一套。
//
// 这里只负责选择与排版：数据已由 `adapters.traceFromMessages` 从**同一份会话消息**摊平，因此轨迹与
// 对话两个页签不可能互相矛盾。

import { useState } from 'react'

import type { TraceRowView, TraceView as TraceViewModel } from '../../fixtures/types'
import { TOOL_STATUS_LABEL, toolLabel } from '../tool/display'
import { resultSummary } from './adapters'
import { TraceInspector } from './TraceInspector'
import styles from './TraceView.module.css'

/** 契约没给、或那一支确实没有时的统一说法。**绝不写 0**。 */
const UNKNOWN = '未知'

const STATUS_CLASS: Record<TraceRowView['status'], string> = {
  pending: styles.statusPending ?? '',
  running: styles.statusRunning ?? '',
  succeeded: styles.statusSucceeded ?? '',
  failed: styles.statusFailed ?? '',
}

interface TraceViewProps {
  trace: TraceViewModel
}

export function TraceView({ trace }: TraceViewProps) {
  // 选中态是**界面状态**，不属于服务端契约：刷新后不保留，也不假装是服务端的选择。
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = trace.rows.find((row) => row.id === selectedId) ?? null

  return (
    <div className={styles.split}>
      <div className={styles.tablePane} role="region" aria-label="轨迹">
        {trace.rows.length === 0 ? (
          <p className={styles.empty}>
            这个会话还没有操作记录。
          </p>
        ) : (
          <table className={styles.table}>
            <caption className={styles.caption}>
              按发生顺序记录的操作。选择一行查看结果和证据。
            </caption>
            <thead>
              <tr>
                <th scope="col" className={styles.colType}>
                  操作
                </th>
                <th scope="col" className={styles.colPayload}>
                  目标
                </th>
                <th scope="col" className={styles.colResult}>
                  结果
                </th>
                <th scope="col" className={styles.colTime}>
                  耗时
                </th>
              </tr>
            </thead>
            <tbody>
              {trace.rows.map((row) => {
                const isSelected = row.id === selectedId
                const summary = resultSummary(row.resultPreview)
                return (
                  <tr
                    key={row.id}
                    className={isSelected ? `${styles.row} ${styles.rowSelected}` : styles.row}
                    data-trace-row={row.id}
                    data-selected={isSelected ? 'true' : 'false'}
                    onClick={() => {
                      setSelectedId(row.id)
                    }}
                  >
                    <td className={styles.typeCell}>
                      {/* 行级点击负责鼠标，这个按钮负责键盘：两者指向同一次选择。 */}
                      <button
                        type="button"
                        className={styles.typeButton}
                        aria-pressed={isSelected}
                        aria-label={`查看${toolLabel(row.toolName)}（${TOOL_STATUS_LABEL[row.status]}）的详情`}
                        onClick={(event) => {
                          event.stopPropagation()
                          setSelectedId(row.id)
                        }}
                      >
                        <span className={`${styles.status} ${STATUS_CLASS[row.status]}`} aria-hidden="true" />
                        {toolLabel(row.toolName)}
                      </button>
                    </td>
                    <td className={styles.payloadCell}>
                      {row.target ?? UNKNOWN}
                    </td>
                    <td className={styles.resultCell}>
                      {summary === '' ? UNKNOWN : summary}
                      {row.resultTruncated ? <span className={styles.truncated}>（已截断）</span> : null}
                    </td>
                    <td className={styles.timeCell}>{row.durationLabel === '' ? UNKNOWN : row.durationLabel}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className={styles.inspectorPane}>
        {selected === null ? (
          <p className={styles.hint}>选择左侧操作，查看结果和证据。</p>
        ) : (
          <TraceInspector row={selected} />
        )}
      </div>
    </div>
  )
}
