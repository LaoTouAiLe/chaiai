// 轨迹检查器（matrix 21）：一行工具调用的五段详情。
//
// 排版照参考的 Summary / Payload / Result / Schema / Timing，但内容只写契约里**真的有**的东西：
// 拿不到的（所属轮次、绑定的模型调用、供应商未上报的 token）写"未知"，不写 0、不编哈希、也不为了
// 看着完整去拆自然语言。

import type { ReactNode } from 'react'

import type { ModelCallView, TraceRowView } from '../../fixtures/types'
import { TOOL_STATUS_LABEL } from '../tool/ToolRow'
import { parameterSummary } from './adapters'
import styles from './TraceView.module.css'

/** 契约没给、或那一支确实没有时的统一说法。**绝不写 0**：0 是一个事实，未知是另一个事实。 */
const UNKNOWN = '未知'

interface TraceInspectorProps {
  row: TraceRowView
  /** 会话级模型调用；Schema 段用（没有单条工具到模型调用的绑定字段）。 */
  calls: readonly ModelCallView[]
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <span className={styles.fieldLabel}>{label}</span>
      <span className={styles.fieldValue}>{children}</span>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={styles.section} aria-label={title}>
      <h3 className={styles.sectionTitle}>{title}</h3>
      {children}
    </section>
  )
}

export function TraceInspector({ row, calls }: TraceInspectorProps) {
  const payload = parameterSummary(row.parameters)
  const result = row.resultPreview

  return (
    <div className={styles.inspector} role="region" aria-label="轨迹检查器">
      <p className={styles.inspectorHead}>
        {row.toolName} · {TOOL_STATUS_LABEL[row.status]}
      </p>

      <Section title="Summary">
        <Field label="工具">{row.toolName}</Field>
        <Field label="状态">{TOOL_STATUS_LABEL[row.status]}</Field>
        <Field label="目标">{row.target ?? UNKNOWN}</Field>
        <Field label="所属轮次">{UNKNOWN}</Field>
        <Field label="证据">
          {row.evidence === null
            ? UNKNOWN
            : `${row.evidence.evidenceType}：${row.evidence.summary} · ${row.evidence.verified ? '已机械校验' : '未通过机械校验'
            }`}
        </Field>
        {/* 两处"未知"是契约的缺口，不是本页没渲染：说清楚，免得被读成"这里没执行过"。 */}
        <p className={styles.note}>
          契约没有记录单条工具调用属于哪一轮（`ToolUseView` 没有 `run_id`），也没有把它绑定到产生它的
          模型调用。这两处因此按未知呈现，不猜归属。
        </p>
      </Section>

      <Section title="Payload">
        {row.parameters.length === 0 ? (
          <>
            <p className={styles.value}>{UNKNOWN}</p>
            <p className={styles.note}>
              没有可展示的参数记录。契约无法区分"这次调用本来就没有参数"与"参数未记录"，所以不写"无参数"。
            </p>
          </>
        ) : (
          <ul className={styles.pairs}>
            {row.parameters.map(({ name, value }) => (
              <li key={name} className={styles.pair}>
                <span className={styles.pairName}>{name}</span>
                <span className={styles.pairValue}>{value}</span>
              </li>
            ))}
          </ul>
        )}
        {payload === '' ? null : <p className={styles.note}>以上为已脱敏参数，界面永远看不到原文。</p>}
      </Section>

      <Section title="Result">
        <Field label="结果摘要">{result === '' ? UNKNOWN : result}</Field>
        <Field label="结果指纹">{row.resultDigest ?? UNKNOWN}</Field>
        <Field label="是否截断">{row.resultTruncated ? '已截断' : '未截断'}</Field>
        {row.resultTruncated ? (
          <p className={styles.note}>预览被服务端截断；完整结果保存在会话事件里，本页不显示未脱敏正文。</p>
        ) : null}
      </Section>

      <Section title="Schema">
        {calls.length === 0 ? (
          <p className={styles.value}>{UNKNOWN}</p>
        ) : (
          <ul className={styles.calls}>
            {calls.map((call) => (
              <li key={call.id} className={styles.call}>
                <span className={styles.callPurpose}>{call.purpose}</span>
                <span className={styles.callModel}>{call.modelLabel}</span>
                <span className={styles.callFingerprint}>
                  工具 Schema 指纹：{call.schemaFingerprint ?? UNKNOWN}
                </span>
                <span className={styles.callNumbers}>
                  输入 {call.inputTokens === null ? UNKNOWN : String(call.inputTokens)} · 输出{' '}
                  {call.outputTokens === null ? UNKNOWN : String(call.outputTokens)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className={styles.note}>
          按会话列出模型调用：契约没有"哪次工具调用由哪次模型调用产生"的绑定字段，所以这里不给单条调用
          指定归属。指纹取自该次调用实际发送的工具 Schema；未上报的 token 按未知显示。
        </p>
      </Section>

      <Section title="Timing">
        <Field label="开始">{row.startedLabel === '' ? UNKNOWN : row.startedLabel}</Field>
        <Field label="结束">{row.finishedLabel === '' ? UNKNOWN : row.finishedLabel}</Field>
        <Field label="耗时">{row.durationLabel === '' ? UNKNOWN : row.durationLabel}</Field>
        <p className={styles.note}>耗时由服务端按调用开始与结束时刻计算；没有可解析时刻时按未知显示。</p>
      </Section>
    </div>
  )
}