import { Fragment, useEffect, useRef } from 'react'

import { IconCopyButton, MarkdownMessage } from './MarkdownMessage'
import { ErrorNotice } from '../error/ErrorNotice'
import { MemoryRow } from '../memory/MemoryRow'
import { ToolRow } from '../tool/ToolRow'
import type {
  ConversationScenario,
  MessageView,
  ModelCallView,
  TurnStatsView,
} from '../../fixtures/types'
import styles from './Conversation.module.css'

interface ConversationProps {
  scenario: ConversationScenario
  /** 真会话只在用户停留底部时跟随增量；原型基线不自动改滚动位置。 */
  autoFollow?: boolean
  /**
   * 展开/收起一行工具结果（T32）。
   *
   * 展开态是**界面状态**，不属于服务端契约，所以它由容器注入：原型 fixture 的 `expanded`
   * 直接写在数据里，真会话则由容器按被点过的行决定。省略即原型行为。
   */
  onToggleTool?: (messageId: string) => void
}

function UserMessage({ text, at }: { text: string; at: string }) {
  return (
    <div className={styles.userRow}>
      <div className={styles.userBubble}>{text}</div>
      <span className={styles.userTime}>{at}</span>
    </div>
  )
}

function AssistantMessage({ text, at }: { text: string; at: string }) {
  return (
    <div className={styles.assistantRow}>
      {/* 正文按 Markdown 渲染（T30）：**不**解析原始 HTML，代码块自带复制。
          用 div 而不是 p：Markdown 会输出自己的块级元素，p 里嵌 p 是非法结构。 */}
      <div className={styles.assistantText}>
        <MarkdownMessage text={text} />
      </div>
      <div className={styles.actions}>
        <IconCopyButton text={text} className={styles.action} />
        <span className={styles.assistantTime}>{at}</span>
      </div>
    </div>
  )
}

function formatTokens(value: number): string {
  return value >= 1_000 ? `${(value / 1000).toFixed(1)}k tok` : `${String(value)} tok`
}

function sumOf(calls: readonly ModelCallView[], pick: (call: ModelCallView) => number | null): number | null {
  let total = 0
  for (const call of calls) {
    const value = pick(call)
    if (value === null) return null
    total += value
  }
  return calls.length > 0 ? total : null
}

/**
 * Aggregate the turn from its per-call records.
 *
 * The summary is always the deterministic sum of the details, never a number
 * carried alongside them (ARCHITECTURE §15.6), so the two can never disagree.
 */
function aggregate(stats: TurnStatsView): { input: number | null; output: number | null } {
  if (stats.calls.length === 0) return { input: stats.inputTokens, output: stats.outputTokens }
  return {
    input: sumOf(stats.calls, (call) => call.inputTokens),
    output: sumOf(stats.calls, (call) => call.outputTokens),
  }
}

/**
 * Per-turn statistics line.
 *
 * Unknown values print as unknown — never as `0` — because the provider either
 * reports cache usage or it does not. Estimated token numbers are labelled.
 */
function TurnStats({ stats }: { stats: TurnStatsView }) {
  const { input, output } = aggregate(stats)
  return (
    <div className={styles.stats}>
      <span>
        {stats.turns} 轮 · {stats.steps} 步
      </span>
      <span className={styles.statsDivider} aria-hidden="true">
        |
      </span>
      <span>耗时 {stats.elapsedLabel}</span>
      <span className={styles.statsDivider} aria-hidden="true">
        |
      </span>
      <span>
        缓存命中{' '}
        {stats.cacheHitRatio === null ? '未知' : `${String(Math.round(stats.cacheHitRatio * 100))}%`}
      </span>
      <span className={styles.statsDivider} aria-hidden="true">
        |
      </span>
      <span>
        输入 {input === null ? '未知' : formatTokens(input)} · 输出 {output === null ? '未知' : formatTokens(output)}
        {stats.estimated ? <span className={styles.estimated}>估算</span> : null}
      </span>

      {stats.calls.length > 0 ? (
        <details className={styles.breakdown}>
          <summary>明细（{stats.calls.length} 次模型调用）</summary>
          <ul className={styles.callList}>
            {stats.calls.map((call) => (
              <li key={call.id} className={styles.callRow}>
                <span className={styles.callPurpose}>{call.purpose}</span>
                <span className={styles.callModel}>{call.modelLabel}</span>
                <span className={styles.callNumbers}>
                  输入 {call.inputTokens === null ? '未知' : formatTokens(call.inputTokens)} · 输出{' '}
                  {call.outputTokens === null ? '未知' : formatTokens(call.outputTokens)}
                </span>
                <span className={styles.callElapsed}>{call.elapsedLabel}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}

function Message({
  message,
  onToggleTool,
}: {
  message: MessageView
  onToggleTool?: (messageId: string) => void
}) {
  switch (message.kind) {
    case 'user':
      return <UserMessage text={message.text} at={message.at} />
    case 'assistant':
      return <AssistantMessage text={message.text} at={message.at} />
    case 'tool':
      return (
        <div className={styles.toolSlot}>
          <ToolRow
            tool={message.tool}
            onToggle={
              onToggleTool === undefined ? undefined : () => onToggleTool(message.id)
            }
          />
        </div>
      )
    case 'error':
      return (
        <div className={styles.errorSlot}>
          <ErrorNotice notice={message.notice} />
        </div>
      )
    case 'memory':
      return (
        <div className={styles.memorySlot}>
          <MemoryRow memory={message.memory} />
        </div>
      )
  }
}

/**
 * The conversation column: message flow plus the closing statistics line.
 *
 * Loading and empty replace the flow rather than sitting above it: a skeleton
 * next to real messages would claim both are the same kind of thing.
 */
export function Conversation({ scenario, autoFollow = false, onToggleTool }: ConversationProps) {
  const loading = scenario.loading ?? null
  const empty = scenario.empty ?? null
  const scrollRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const turnStats = new Map((scenario.turnStats ?? []).map((item) => [item.afterMessageId, item.stats]))

  useEffect(() => {
    if (autoFollow) followRef.current = true
  }, [autoFollow, scenario.sessionId])

  useEffect(() => {
    if (!autoFollow || !followRef.current) return
    const element = scrollRef.current
    if (element !== null) element.scrollTop = element.scrollHeight
  }, [autoFollow, scenario.messages])

  return (
    <div
      className={styles.scroll}
      role="log"
      aria-label="对话"
      ref={scrollRef}
      onScroll={autoFollow ? (event) => {
        const element = event.currentTarget
        followRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80
      } : undefined}
    >
      <div className={styles.column}>
        {loading !== null ? (
          <div className={styles.loading}>
            <p className={styles.loadingLabel}>{loading.label}</p>
            <p className={styles.loadingDetail}>{loading.detail}</p>
            {/* Shape only: the skeleton must not invent message content. */}
            <div className={styles.skeleton} aria-hidden="true">
              {loading.rows.map((width, index) => (
                <span key={index} className={styles.skeletonRow} style={{ width: `${String(width)}%` }} />
              ))}
            </div>
          </div>
        ) : null}

        {loading === null && empty !== null ? (
          <div className={styles.empty}>
            <p className={styles.emptyTitle}>{empty.title}</p>
            <p className={styles.emptyText}>{empty.description}</p>
          </div>
        ) : null}

        {loading === null && empty === null ? (
          <>
            {scenario.messages.map((message) => (
              <Fragment key={message.id}>
                <Message message={message} onToggleTool={onToggleTool} />
                {turnStats.has(message.id) ? <TurnStats stats={turnStats.get(message.id)!} /> : null}
              </Fragment>
            ))}
            {scenario.stats === null ? null : <TurnStats stats={scenario.stats} />}
          </>
        ) : null}
      </div>
    </div>
  )
}
