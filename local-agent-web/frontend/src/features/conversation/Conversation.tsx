import { Fragment, useEffect, useRef } from 'react'

import { IconCopyButton, MarkdownMessage } from './MarkdownMessage'
import { RunActivity } from './RunActivity'
import { ReasoningRow } from './ReasoningRow'
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
  focusTurnId?: string | null
}

function UserMessage({ text, at }: { text: string; at: string }) {
  return (
    <div className={styles.userRow}>
      <div className={styles.userBubble}>{text}</div>
      <span className={styles.userTime}>{at}</span>
    </div>
  )
}

function AssistantMessage({ text, at, reasoning, reasoningTruncated, reasoningRunning, streaming }:
  Extract<MessageView, { kind: 'assistant' }>) {
  return (
    <div className={styles.assistantRow}>
      {reasoning ? <ReasoningRow text={reasoning} running={reasoningRunning ?? false} truncated={reasoningTruncated} /> : null}
      {/* 正文按 Markdown 渲染（T30）：**不**解析原始 HTML，代码块自带复制。
          用 div 而不是 p：Markdown 会输出自己的块级元素，p 里嵌 p 是非法结构。 */}
      {text !== '' ? <div className={styles.assistantText} data-streaming={streaming || undefined}>
        <MarkdownMessage text={text} />
      </div> : null}
      {/* 复制按钮已上移到"每轮一组"（`TurnActions`）：原来每个助手段落各一个，一轮下来七八个
          重复按钮。这里只留时间。 */}
      {at !== '' ? <div className={styles.actions}>
        <span className={styles.assistantTime}>{at}</span>
      </div> : null}
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
 * 2026-09-30 改为**紧凑单行**：原来是一个折叠块，标题只写"本轮记录 · 耗时 Xs"，模型/工具耗时、
 * 首 token、吞吐这些本来就在契约里的数字（`TurnRecordView.model_elapsed_ms` /
 * `tool_elapsed_ms` / `time_to_first_token_ms`）根本没显示；而且统计块按锚点各渲染一份，一轮里
 * 会出现两三次。现在关键数字直接摊在一行，明细降到二级折叠，且一轮只渲染一份。
 *
 * Unknown values print as unknown — never as `0` — because the provider either
 * reports cache usage or it does not. Estimated token numbers are labelled.
 */
function TurnStats({ stats }: { stats: TurnStatsView }) {
  const { input, output } = aggregate(stats)
  const unknown = '未知'
  // 五个新字段类型上是可选的（原型 fixture 早于它们存在），这里统一归一，别让 `undefined`
  // 掉进 `=== null` 的判断里被当成"有值"。
  const throughput = stats.throughput ?? null
  // 缓存：**有比率就显示比率**（它本身就是事实）；只有比率缺失时才去区分"供应商上报了但没给
  // 命中率"（未知）与"这个供应商根本没有缓存字段"（未上报）。反过来先用 `cacheReported` 当门
  // 会把已有的比率藏掉——原型 fixture 没有该标记，页面就会把 62% 显示成"未上报"。
  const cache =
    stats.cacheHitRatio !== null
      ? `${String(Math.round(stats.cacheHitRatio * 100))}%`
      : (stats.cacheReported ?? false)
        ? unknown
        : '未上报'
  return (
    <details className={styles.stats}>
      <summary className={styles.statsSummary}>
        <span>本轮记录 · 整轮 {stats.elapsedLabel || unknown}</span>
        <span>缓存命中 {cache}</span>
      </summary>
      <div className={styles.statsDetails}>
        <span>{stats.turns} 轮 · {stats.steps} 步</span>
        <span>LLM {stats.modelElapsedLabel || unknown}</span>
        <span>工具 {stats.toolElapsedLabel || unknown}</span>
        <span>首 token {stats.firstTokenLabel || unknown}</span>
        <span>{throughput === null ? `吞吐 ${unknown}` : `${String(throughput)} tok/s`}</span>
        <span>
          输入 {input === null ? unknown : formatTokens(input)} · 输出 {output === null ? unknown : formatTokens(output)}
          {stats.estimated ? <span className={styles.estimated}>估算</span> : null}
        </span>
      </div>

      {stats.calls.length > 0 ? (
        <details className={styles.breakdown}>
          <summary>明细（{stats.calls.length} 次模型调用 · 整轮 {stats.elapsedLabel || unknown}）</summary>
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
    </details>
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
      return <AssistantMessage {...message} />
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

/** 一轮：一条用户消息到下一次用户消息之前（列首/列尾也各算一轮）。 */
interface Turn {
  /** React key：轮首消息 id。 */
  id: string
  rows: MessageView[]
  /** 该轮全部助手正文，供"复制整轮回答"用。 */
  answerParts: string[]
  /** 该轮最后一条消息 id：统计行按它锚定（`mapTurnStats` 用的是同一口径）。 */
  lastId: string
}

/** 按用户轮次保留消息顺序；工具行各自折叠，不跨助手正文挪动动作。 */
function partitionTurns(messages: readonly MessageView[]): Turn[] {
  const turns: Turn[] = []
  for (const message of messages) {
    if (turns.length === 0 || message.kind === 'user') {
      turns.push({ id: message.id, rows: [], answerParts: [], lastId: message.id })
    }
    const turn = turns.at(-1)!
    turn.lastId = message.id
    if (message.kind === 'assistant') turn.answerParts.push(message.text)
    turn.rows.push(message)
  }
  return turns
}

/** 每轮**一组**操作按钮（不再每个助手段落一个）。整轮没有正文时不渲染。 */
function TurnActions({ text }: { text: string }) {
  if (text.trim() === '') return null
  return (
    <div className={styles.actions}>
      <IconCopyButton text={text} className={styles.action} />
    </div>
  )
}

/**
 * The conversation column: message flow plus the closing statistics line.
 *
 * Loading and empty replace the flow rather than sitting above it: a skeleton
 * next to real messages would claim both are the same kind of thing.
 */
export function Conversation({ scenario, autoFollow = false, onToggleTool, focusTurnId }: ConversationProps) {
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
  }, [autoFollow, scenario.messages, scenario.activity?.id, scenario.activity?.label])

  useEffect(() => {
    if (!focusTurnId) return
    const target = Array.from(scrollRef.current?.querySelectorAll<HTMLElement>('[data-turn-id]') ?? [])
      .find((element) => element.dataset.turnId === focusTurnId)
    if (target !== undefined) {
      followRef.current = false
      target.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
    }
  }, [focusTurnId, scenario.messages])

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
            {partitionTurns(scenario.messages).map((turn) => {
              const stats = turnStats.get(turn.lastId)
              return (
                <div key={turn.id} data-turn-id={turn.id} className={styles.turn}>
                  {turn.rows.map((row) => (
                    <Fragment key={row.id}>
                      <Message message={row} onToggleTool={onToggleTool} />
                    </Fragment>
                  ))}
                  <TurnActions text={turn.answerParts.join('\n\n')} />
                  {stats === undefined ? null : <TurnStats stats={stats} />}
                </div>
              )
            })}
            {scenario.stats === null ? null : <TurnStats stats={scenario.stats} />}
            {scenario.activity == null ? null : <RunActivity key={scenario.activity.id} activity={scenario.activity} />}
          </>
        ) : null}
      </div>
    </div>
  )
}
