import type { ReactNode } from 'react'

import type { TraceRowView } from '../../fixtures/types'
import { TOOL_STATUS_LABEL, parameterLabel, toolLabel } from '../tool/display'
import styles from './TraceView.module.css'

const UNKNOWN = '未知'

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      <span className={styles.fieldValue}>{children}</span>
    </div>
  )
}

export function TraceInspector({ row }: { row: TraceRowView }) {
  return (
    <div className={styles.inspector} role="region" aria-label="轨迹检查器">
      <p className={styles.inspectorHead}>
        {toolLabel(row.toolName)} · {TOOL_STATUS_LABEL[row.status]}
      </p>

      <section className={styles.section} aria-label="操作详情">
        <h3 className={styles.sectionTitle}>操作详情</h3>
        <Field label="目标">{row.target ?? UNKNOWN}</Field>
        <Field label="结果">{row.resultPreview === '' ? UNKNOWN : row.resultPreview}</Field>
        {row.resultTruncated ? <p className={styles.note}>结果预览已截断。</p> : null}
        {row.evidence === null ? null : (
          <Field label="证据">
            {row.evidence.summary} · {row.evidence.verified ? '已验证' : '未验证'}
          </Field>
        )}
      </section>

      {row.parameters.length > 0 ? (
        <details className={styles.section}>
          <summary className={styles.sectionTitle}>查看参数</summary>
          <ul className={styles.pairs}>
            {row.parameters.map(({ name, value }) => (
              <li key={name} className={styles.pair}>
                <span className={styles.pairName}>{parameterLabel(name)}</span>
                <span className={styles.pairValue}>{value}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      <section className={styles.section} aria-label="时间">
        <h3 className={styles.sectionTitle}>时间</h3>
        <Field label="开始">{row.startedLabel || UNKNOWN}</Field>
        <Field label="结束">{row.finishedLabel || UNKNOWN}</Field>
        <Field label="耗时">{row.durationLabel || UNKNOWN}</Field>
      </section>
    </div>
  )
}
