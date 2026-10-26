import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import { ConversationRail } from './ConversationRail'
import type { RailQuestion } from './ConversationRail'
import { Bulb, Pencil, Quote as QuoteIcon } from '../../components/icons'
import { ScrollToBottom } from '../../components/ScrollToBottom'
import { IconCopyButton, MarkdownMessage } from './MarkdownMessage'
import { splitQuotedMessage } from './quote'
import { splitSideChatPrompt } from './sideChat'
import { RunActivity } from './RunActivity'
import { ReasoningRow } from './ReasoningRow'
import { WebSources } from './WebSources'
import { CompressionRow } from './CompressionRow'
import { ImageLightbox } from './ImageLightbox'
import { ErrorNotice } from '../error/ErrorNotice'
import { MemoryRow } from '../memory/MemoryRow'
import { ToolRow } from '../tool/ToolRow'
import { toolLabel } from '../tool/display'
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
  errorActions?: ErrorActions
  /** 编辑并重发（2026-10-21）。省略即原型态：用户消息上不出现「编辑」按钮。 */
  onEditMessage?: EditMessageHandler
  /**
   * 每轮末尾的改动摘要（2026-10-21）。
   *
   * 由容器注入而不是写在组件里：这一块的**数据**来自变更账本（React Query），而对话组件
   * 一直是纯展示、只认 view model。省略即不渲染（地图抽屉里的复用的对话就不注入）。
   */
  renderTurnChanges?: (turnId: string) => ReactNode
  /** 把正在跑的命令转入后台（2026-10-21）。省略即原型行为：行上没有这个按钮。 */
  onDetachCommand?: (commandId: string) => Promise<boolean>
  /**
   * 引用一段回答继续追问（2026-10-26）。省略即原型态：轮尾没有「追问」按钮、拖选也不浮按钮。
   *
   * 只传文本，不传"这是哪条消息"：引用最终会拼进用户消息正文，界面与契约都不需要区分来源。
   */
  onQuote?: (text: string) => void
}

export interface ErrorActions {
  dismissedIds: ReadonlySet<string>
  onToggle: (diagnosticId: string) => void
}

/**
 * 「编辑并重发」的回调：宿主据此从**上一轮之后**重新提交这段文字。
 *
 * 返回 `true` = 提交被接受（可以退出编辑态）；`false` = 被拒（一轮在跑、待审批等）。
 * 组件据此**留在编辑态**，不把用户刚改的词吞掉。契约与 `Composer.onSend` 一致。
 */
export type EditMessageHandler = (
  parentTurnId: string | null,
  text: string,
  attachments: readonly string[],
) => boolean

function UserMessage({
  text,
  at,
  images,
  parentTurnId,
  onEdit,
}: {
  text: string
  at: string
  images?: readonly string[]
  parentTurnId: string | null
  onEdit?: EditMessageHandler
}) {
  const pictures = images ?? []
  // 点开看原图（2026-10-16）：缩略图有尺寸上限（220×160），截图里的小字、UI 细节得放大才看得清。
  const [zoomed, setZoomed] = useState<string | null>(null)
  // **就地编辑**（2026-10-21）：`null` = 不在编辑态。改动发生在这一条气泡上，不碰底部输入框——
  // 用户改的是"已经发出去的那句话"，不是要写一句新的。
  const [draft, setDraft] = useState<string | null>(null)
  // 提交成功后到新快照到达之间，气泡里显示的仍是**旧** `text`——用户会以为"我改的没生效"。
  // 用一个本地覆盖顶上，等新快照的文本进来再放开。
  const [submitted, setSubmitted] = useState<string | null>(null)

  useEffect(() => {
    setSubmitted(null)
  }, [text])

  const submit = () => {
    if (onEdit === undefined || draft === null) return
    // 与输入框同一条口径：只发图也算合法提交，所以空判要看图片。
    if (draft.trim() === '' && pictures.length === 0) return
    if (onEdit(parentTurnId, draft, pictures)) {
      setSubmitted(draft)
      setDraft(null)
    }
  }
  const shown = submitted ?? text
  // 侧聊首轮：把注入的上下文折起来，只显示用户真正问的那句（§21.7）。
  const { context: injectedContext, question: shownQuestion } = splitSideChatPrompt(shown)
  // 追问引用（2026-10-26）：引用是消息正文的一部分，气泡必须把它和用户自己写的问题分开显示，
  // 否则那段 `>` 看起来就是用户打的字。
  const { quote: quoted, question: askedText } = splitQuotedMessage(shownQuestion)

  return (
    <div className={styles.userRow}>
      {draft === null ? (
        <>
          <div className={styles.userBubble}>
            {/* 只发图不发字是**合法提交**（2026-10-16）：那时气泡里只有图，不渲染一个空段落。 */}
            {injectedContext === '' ? null : (
              <details className={styles.userContext}>
                <summary className={styles.userContextSummary}>来自主会话的上下文</summary>
                <pre className={styles.userContextBody}>{injectedContext}</pre>
              </details>
            )}
            {/* 有引用时先摆引用块、再摆用户自己写的问题（2026-10-26）。分开渲染的理由不是好看：
                引用是**从旧回答里摘出来的内容**，混在正文里会被读成"用户自己说的话"。 */}
            {shownQuestion === '' ? null : quoted === null ? shownQuestion : (
              <>
                <blockquote className={styles.userQuote}>{quoted}</blockquote>
                {askedText === '' ? null : <div className={styles.userQuestion}>{askedText}</div>}
              </>
            )}
            {pictures.length === 0 ? null : (
              <ul className={styles.userImages} aria-label="随这条消息发送的图片">
                {pictures.map((src) => (
                  <li key={src} className={styles.userImage}>
                    <button
                      type="button"
                      className={styles.userImageButton}
                      aria-label="放大查看这张图片"
                      onClick={() => setZoomed(src)}
                    >
                      <img src={src} alt="随消息发送的图片" loading="lazy" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {/* 复制与编辑（2026-10-21）：只出现在用户自己的消息上，且**只有图标**——
              文字按钮会跟时间戳挤在同一行。 */}
          <div className={styles.userActions}>
            <IconCopyButton text={shownQuestion} className={styles.userAction} />
            {/* 带引用的消息**不给编辑**（2026-10-26）：编辑框是纯文本 textarea，只能把
                `> ……` 原文摆出来——那段引用会重新变成"用户自己打的字"，正是气泡里刚分清楚的东西。
                要编辑就重发一条，或者复制后自己改。 */}
            {onEdit === undefined || quoted !== null ? null : (
              <button
                type="button"
                className={styles.userAction}
                title="编辑后重新发送"
                aria-label="编辑这条提问"
                onClick={() => setDraft(shownQuestion)}
              >
                <Pencil />
              </button>
            )}
          </div>
        </>
      ) : (
        <div className={styles.userEditBox}>
          <textarea
            className={styles.userEditInput}
            value={draft}
            rows={Math.min(10, Math.max(2, draft.split('\n').length))}
            aria-label="编辑这条提问"
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              // Esc 取消、Enter 发送（Shift+Enter 换行）——与底部输入框同一套习惯。
              if (event.key === 'Escape') {
                event.preventDefault()
                setDraft(null)
              } else if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                submit()
              }
            }}
          />
          {pictures.length === 0 ? null : (
            <p className={styles.userEditNote}>这条消息的 {pictures.length} 张图片会一起带上</p>
          )}
          <div className={styles.userEditActions}>
            <button
              type="button"
              className={styles.editCancel}
              title="取消（Esc）"
              onClick={() => setDraft(null)}
            >
              取消
            </button>
            <button
              type="button"
              className={styles.editSend}
              title="发送（Enter）"
              disabled={draft.trim() === '' && pictures.length === 0}
              onClick={submit}
            >
              发送
            </button>
          </div>
        </div>
      )}
      <span className={styles.userTime}>{at}</span>
      {zoomed === null ? null : (
        <ImageLightbox src={zoomed} onClose={() => setZoomed(null)} />
      )}
    </div>
  )
}

/**
 * 助手身份行：产品标记 + 名字（2026-10-21）。一轮只出现一次（见 `replyStart`）。
 *
 * 显示**产品名**而不是模型名：模型随时可切，而这行回答的是"这段是谁说的"。
 * 图标复用侧栏那个 `public/chaiai.svg`——同一个产品只该有一个标记，不另画一个。
 */
function AssistantIdentity() {
  return (
    <div className={styles.identity}>
      {/* `alt` 留空：紧跟其后的文字已经是同一个名字，读屏不需要听两遍。 */}
      <img className={styles.identityMark} src="/chaiai.svg" width={18} height={18} alt="" />
      <span className={styles.identityName}>chaiai</span>
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
      {/* `data-quotable`（2026-10-26）：拖选浮层只认这个标记内的选区——工具行、来源列表里
          选中的文字不属于"回答正文"，不该弹出追问。 */}
      {text !== '' ? <div className={styles.assistantText} data-streaming={streaming || undefined} data-quotable>
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
  errorActions,
  parentTurnId,
  onEdit,
  onDetachCommand,
}: {
  message: MessageView
  onToggleTool?: (messageId: string) => void
  errorActions?: ErrorActions
  /** 这一轮之前的那一轮（没有就是会话开头）；编辑重发要退到这里。 */
  parentTurnId?: string | null
  onEdit?: EditMessageHandler
  onDetachCommand?: (commandId: string) => Promise<boolean>
}) {
  switch (message.kind) {
    case 'user':
      return (
        <UserMessage
          text={message.text}
          at={message.at}
          images={message.attachments}
          parentTurnId={parentTurnId ?? null}
          onEdit={onEdit}
        />
      )
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
            onDetach={onDetachCommand}
          />
        </div>
      )
    case 'error':
      // **工具类失败不再单独占一块横幅**（2026-10-16，用户实拍反馈）：那一次失败的 `×` 与
      // 「结果详情」就在上面的工具行里，横幅只是把同一件事再说一遍、还占掉半屏（"执行操作打叉就可以了"）。
      // 其余类别（配置 / 模型 / 安全 / 审批 / 恢复 / 系统）照旧显示——它们没有别的出口。
      // 注意这只跳过**渲染**：消息本身仍在流与持久化里，诊断信息一条都没丢。
      if (message.notice.category === 'tool') return null
      return (
        <div className={styles.errorSlot}>
          <ErrorNotice notice={message.notice} dismissed={errorActions?.dismissedIds.has(message.notice.diagnosticId)}
            onToggleDismissed={errorActions === undefined ? undefined : () => errorActions.onToggle(message.notice.diagnosticId)} />
        </div>
      )
    case 'compression':
      // 上下文压缩是**流程的一步**（2026-10-16）：与 Think、工具行同级，排在它真正发生的时刻。
      return <CompressionRow compression={message.compression} />
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

/** 只读工具：连续出现时合并成一行摘要（2026-10-22）。写类工具不参与，每条都要可追溯。 */
const READ_ONLY_TOOL_NAMES = new Set([
  'list_dir', 'read_file', 'grep', 'glob', 'search_files', 'view_image',
  'git_status', 'git_diff', 'git_log', 'skill_list', 'skill_load',
])

type TurnGroup =
  | { kind: 'single'; row: MessageView }
  | { kind: 'tools'; rows: MessageView[] }

/** 把一轮里的消息切块：连续只读工具成组，其余逐个。 */
function groupRows(rows: MessageView[]): TurnGroup[] {
  const groups: TurnGroup[] = []
  let buffer: MessageView[] = []
  const flush = () => {
    if (buffer.length > 0) groups.push({ kind: 'tools', rows: buffer })
    buffer = []
  }
  for (const row of rows) {
    if (row.kind === 'tool' && row.tool !== undefined && READ_ONLY_TOOL_NAMES.has(row.tool.name)) {
      buffer.push(row)
      continue
    }
    flush()
    groups.push({ kind: 'single', row })
  }
  flush()
  return groups
}

function summarizeTools(rows: MessageView[]): string {
  const counts = new Map<string, number>()
  for (const row of rows) {
    const label = row.kind === 'tool' ? toolLabel(row.tool.name) : '工具'
    counts.set(label, (counts.get(label) ?? 0) + 1)
  }
  return [...counts].slice(0, 3).map(([label, count]) => `${label} ${count} 次`).join(' · ')
}

/**
 * 一组连续只读工具：默认一行摘要，点开才是逐条。
 *
 * 为什么只合只读：读文件/搜索是"同一段调查"的连续动作，逐个铺开时一屏 20 行工具会把
 * 回答挤没；写类工具（改文件、跑命令）每条都要能单独追溯，合并会让"改了几次"变得不可见。
 */
function ToolGroup({
  rows, onToggleTool, onDetachCommand,
}: {
  rows: MessageView[]
  onToggleTool?: (messageId: string) => void
  onDetachCommand?: (commandId: string) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const running = rows.some((row) => row.kind === 'tool' && row.tool.status === 'running')
  const failed = rows.filter((row) => row.kind === 'tool' && row.tool.status === 'failed').length
  return (
    <div className={styles.toolGroup}>
      <button
        type="button"
        className={styles.toolGroupHead}
        aria-expanded={open}
        onClick={() => { setOpen((current) => !current) }}
      >
        <span className={styles.toolGroupChevron} aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className={styles.toolGroupLabel}>{summarizeTools(rows)}</span>
        {running ? <span className={styles.toolGroupRunning}>执行中…</span> : null}
        {failed > 0 ? <span className={styles.toolGroupFailed}>{failed} 个失败</span> : null}
      </button>
      {open ? (
        <div className={styles.toolGroupBody}>
          {rows.map((row) => (
            <Message
              key={row.id}
              message={row}
              onToggleTool={onToggleTool}
              onDetachCommand={onDetachCommand}
            />
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** 每轮**一组**操作按钮（不再每个助手段落一个）。整轮没有正文时不渲染。 */
function TurnActions({ text, onQuote }: { text: string; onQuote?: (text: string) => void }) {
  if (text.trim() === '') return null
  return (
    <div className={styles.actions}>
      <IconCopyButton text={text} className={styles.action} />
      {onQuote === undefined ? null : (
        <button
          type="button"
          className={styles.action}
          title="引用这段回答继续追问"
          aria-label="追问"
          onClick={() => onQuote(text)}
        >
          <QuoteIcon />
        </button>
      )}
    </div>
  )
}

/** 拖选正文后浮出的小工具条：复制这段、引用这段继续追问。 */
function SelectionBar({ picked, onQuote, onDismiss }: {
  picked: PickedSelection
  onQuote: (text: string) => void
  onDismiss: () => void
}) {
  return (
    <div
      className={styles.selectionBar}
      style={{ left: picked.x, top: picked.y }}
      role="toolbar"
      aria-label="选中内容的操作"
      data-quote-bar
      // 按住时不要清掉选区：按钮的 click 依赖浏览器还没把选区收起（虽然文案已经先存了一份）。
      onMouseDown={(event) => event.preventDefault()}
    >
      <IconCopyButton text={picked.text} className={styles.selectionAction} />
      <button
        type="button"
        className={styles.selectionAction}
        title="引用这段内容继续追问"
        // 与轮尾那个「追问」分开命名：两个入口引用范围不同（整条回答 / 选中片段），
        // 读屏与测试都不该拿到两个同名按钮。
        aria-label="引用这段追问"
        onClick={() => {
          onQuote(picked.text)
          onDismiss()
        }}
      >
        <QuoteIcon />
      </button>
    </div>
  )
}

/** 一次拖选的快照：文本在选中当时就固定下来，后续选区被浏览器收起也不影响。 */
interface PickedSelection {
  text: string
  /** 视口坐标：浮层是 `position: fixed`，跟着选区走，不跟文档走。 */
  x: number
  y: number
}

/**
 * 空态的线稿水印：一份文件 → 一条虚线路径 → 带行号的锚点。
 *
 * 只画「读文件、给行号」这一件本产品真做的事，不再借用参考产品的卡通形象与
 * 书页构图；颜色全部取自语义 token，浅色/深色两套主题都成立。纯装饰，
 * 所以 `aria-hidden`，也不抢标题层级。
 */
function EmptyArt() {
  return (
    <svg className={styles.emptyArt} viewBox="0 0 180 150" aria-hidden="true" focusable="false">
      {/* 图纸网格：与页面底纹同一节拍，更淡一档。 */}
      <g stroke="var(--law-border-l1)" strokeWidth="1" strokeDasharray="2 6">
        <path d="M30 0v150M90 0v150M150 0v150M0 30h180M0 90h180" />
      </g>
      {/* 文件：三行内容，末行短一截。 */}
      <rect x="18" y="34" width="46" height="60" rx="6" fill="none"
        stroke="var(--law-accent-500)" strokeWidth="1.5" strokeLinejoin="round" opacity="0.85" />
      <g stroke="var(--law-accent-400)" strokeWidth="1.6" strokeLinecap="round" opacity="0.7">
        <path d="M29 50h24M29 60h20M29 70h13" />
      </g>
      {/* 路径：从文件出发，落到右侧的行号锚点。 */}
      <path className={styles.emptyArtTrace} d="M68 64c16 0 14-24 30-24s14 26 30 26"
        fill="none" stroke="var(--law-accent-400)" strokeWidth="1.4" strokeDasharray="4 5" strokeLinecap="round" />
      {/* 锚点：一圈虚线 + 一个实心点。 */}
      <circle cx="128" cy="66" r="13" fill="none" stroke="var(--law-accent-400)"
        strokeWidth="1.2" strokeDasharray="3 4" opacity="0.8" />
      <circle cx="128" cy="66" r="3.4" fill="var(--law-accent-500)" />
      {/* 行号标签：方括号夹住一道刻度。 */}
      <g stroke="var(--law-text-caption)" strokeWidth="1.2" strokeLinecap="round" opacity="0.6" fill="none">
        <path d="M108 96h-6v18h6M148 96h6v18h-6" />
      </g>
      <path d="M118 105h20" stroke="var(--law-accent-500)" strokeWidth="1.6" strokeLinecap="round" />
      {/* 两处小色点：只取语义色，不新增品牌色。 */}
      <rect x="152" y="24" width="5" height="5" rx="1.2" fill="var(--law-amber-400)" transform="rotate(20 154.5 26.5)" />
      <circle cx="24" cy="118" r="2.4" fill="var(--law-green-500)" opacity="0.75" />
    </svg>
  )
}

/**
 * The conversation column: message flow plus the closing statistics line.
 *
 * Loading and empty replace the flow rather than sitting above it: a skeleton
 * next to real messages would claim both are the same kind of thing.
 */
export function Conversation({ scenario, autoFollow = false, onToggleTool, focusTurnId, errorActions, onEditMessage, renderTurnChanges, onDetachCommand, onQuote }: ConversationProps) {
  const loading = scenario.loading ?? null
  const empty = scenario.empty ?? null
  const scrollRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  /** 一次拖选的快照（2026-10-26）：文本在选中当时就固定下来，之后选区被浏览器收起也不影响。 */
  const [picked, setPicked] = useState<PickedSelection | null>(null)
  const turnStats = new Map((scenario.turnStats ?? []).map((item) => [item.afterMessageId, item.stats]))
  const turns = useMemo(
    () => (loading === null && empty === null ? partitionTurns(scenario.messages) : []),
    [empty, loading, scenario.messages],
  )
  // 刻度条只认"用户提过什么"：每轮第一条是用户消息才算一次提问；只发图的那轮给一个事实标签。
  const questions = useMemo<RailQuestion[]>(() => turns.flatMap((turn) => {
    const first = turn.rows[0]
    if (first === undefined || first.kind !== 'user') return []
    const text = first.text.replace(/\s+/g, ' ').trim()
    return [{
      id: turn.id,
      label: text !== '' ? text : (first.attachments?.length ?? 0) > 0 ? '图片消息' : '空消息',
    }]
  }), [turns])

  // 追问的选区浮层（2026-10-26）。监听挂在 document 上而不是滚动容器上：拖到容器外再松手
  // 也该出按钮，而那种 `mouseup` 只有 document 一定收得到。
  useEffect(() => {
    if (onQuote === undefined) return
    const dismiss = () => setPicked(null)
    const onMouseUp = (event: MouseEvent) => {
      // 点在浮层自己身上：交给它自己的 onClick，别在这里先把状态清掉。
      const target = event.target instanceof Element ? event.target : null
      if (target?.closest('[data-quote-bar]') != null) return
      const selection = window.getSelection()
      const text = selection?.toString().trim() ?? ''
      if (selection === null || text === '' || selection.rangeCount === 0) {
        dismiss()
        return
      }
      const range = selection.getRangeAt(0)
      const anchor = range.commonAncestorContainer
      const element = anchor.nodeType === Node.ELEMENT_NODE
        ? (anchor as Element)
        : anchor.parentElement
      // 只认回答正文：工具行、来源列表里选中的文字不属于"这段回答"。
      if (element === null || element.closest('[data-quotable]') === null) {
        dismiss()
        return
      }
      const rect = range.getBoundingClientRect()
      if (rect.width === 0 && rect.height === 0) {
        dismiss()
        return
      }
      setPicked({ text, x: rect.left + rect.width / 2, y: rect.top - 8 })
    }
    document.addEventListener('mouseup', onMouseUp)
    // 浮层用的是视口坐标：一滚动就会停在错的位置，所以直接收起，不追着选区重算。
    window.addEventListener('scroll', dismiss, true)
    return () => {
      document.removeEventListener('mouseup', onMouseUp)
      window.removeEventListener('scroll', dismiss, true)
    }
  }, [onQuote])

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
    <div className={styles.wrap}>
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
            {/* 底纹与线稿都是装饰：读屏与测试只认下面这块简报。 */}
            <span className={styles.emptyGrid} aria-hidden="true" />
            <EmptyArt />
            <div className={styles.emptyBoard}>
              <p className={styles.emptyMeta} aria-hidden="true">
                <span className={styles.emptyMetaDot} />
                本机工作台
                <span className={styles.emptyMetaRule} />
              </p>
              <h2 className={styles.emptyTitle}>{empty.title}</h2>
              <p className={styles.emptyText}>{empty.description}</p>
              {empty.highlightsTitle === undefined || empty.highlights === undefined ? null : (
                <section className={styles.emptyBrief} aria-label={empty.highlightsTitle}>
                  <div className={styles.emptyBriefHead}>
                    <span className={styles.emptyBriefMark} aria-hidden="true"><Bulb size={14} /></span>
                    <h3 className={styles.emptyBriefTitle}>{empty.highlightsTitle}</h3>
                    <span className={styles.emptyBriefRule} aria-hidden="true" />
                  </div>
                  <ol className={styles.emptyList}>
                    {empty.highlights.map((item, index) => (
                      <li key={item} className={styles.emptyItem}>
                        <span className={styles.emptyIndex} aria-hidden="true">
                          {String(index + 1).padStart(2, '0')}
                        </span>
                        <span className={styles.emptyItemText}>{item}</span>
                      </li>
                    ))}
                  </ol>
                </section>
              )}
              {empty.hint === undefined ? null : <p className={styles.emptyHint}>{empty.hint}</p>}
            </div>
          </div>
        ) : null}

        {loading === null && empty === null ? (
          <>
            {turns.map((turn, index) => {
              const stats = turnStats.get(turn.lastId)
              // 这一轮的父 = 上一轮；编辑时退回"父之后"再发，旧这一轮及其后续进旧分支（不删）。
              const parentTurnId = index === 0 ? null : turns[index - 1].id
              const groups = groupRows(turn.rows)
              // 身份行挂在**第一个包含非用户消息的组**前：分组后不能再按 rowIndex 判断。
              const firstReplyGroup = groups.findIndex((group) => (
                (group.kind === 'single' ? [group.row] : group.rows).some((row) => row.kind !== 'user')
              ))
              return (
                <div key={turn.id} data-turn-id={turn.id} className={styles.turn}>
                  {groups.map((group, groupIndex) => {
                    const single = group.kind === 'single' ? group.row : null
                    const toolRows = group.kind === 'tools' ? group.rows : []
                    const key = single === null
                      ? `tools:${toolRows[0]?.id ?? String(groupIndex)}`
                      : single.id
                    // 只有**最后一轮**能编辑（2026-10-21）：改更早的一轮等于把后面全部作废，
                    // 而用户多半只是想改刚发出去的这句。要看旧轮次请走对话地图的「从这里继续」。
                    const editHandler = index === turns.length - 1 ? onEditMessage : undefined
                    const renderMessage = (row: MessageView) => (
                      <Message
                        message={row}
                        onToggleTool={onToggleTool}
                        errorActions={errorActions}
                        onDetachCommand={onDetachCommand}
                        parentTurnId={parentTurnId}
                        onEdit={editHandler}
                      />
                    )
                    return (
                      <Fragment key={key}>
                        {groupIndex === firstReplyGroup ? <AssistantIdentity /> : null}
                        {single !== null
                          ? renderMessage(single)
                          : toolRows.length === 1
                            ? renderMessage(toolRows[0]!)
                            : (
                              <ToolGroup
                                rows={toolRows}
                                onToggleTool={onToggleTool}
                                onDetachCommand={onDetachCommand}
                              />
                            )}
                      </Fragment>
                    )
                  })}
                  <WebSources rows={turn.rows} answer={turn.answerParts.join('\n\n')} />
                  {/* 这一轮改了哪些文件：先看"做了什么"，再看"花了多少"。 */}
                  {renderTurnChanges === undefined ? null : renderTurnChanges(turn.id)}
                  {/* 轮尾收口（2026-10-22）：记录在左、动作在右，同一行；动作默认透明，悬停才显形。
                      此前它们是两条独立的行，一轮下来多占两份高度，而且和正文同级、抢注意力。 */}
                  {stats === undefined && turn.answerParts.join('').trim() === '' ? null : (
                    <div className={styles.footer}>
                      {stats === undefined ? <span /> : <TurnStats stats={stats} />}
                      <TurnActions text={turn.answerParts.join('\n\n')} onQuote={onQuote} />
                    </div>
                  )}
                </div>
              )
            })}
            {scenario.stats === null ? null : <TurnStats stats={scenario.stats} />}
            {scenario.activity == null ? null : <RunActivity key={scenario.activity.id} activity={scenario.activity} />}
          </>
        ) : null}
      </div>
    </div>
    <ConversationRail
      scrollRef={scrollRef}
      questions={questions}
      onJump={() => { followRef.current = false }}
    />
    {/* 「滑到最底」（2026-10-21）：往上翻历史之后，一键回到最新一轮。
        与自动跟随（`followRef`）不冲突——点它滚到底时 onScroll 会把跟随重新打开。 */}
    <ScrollToBottom paneRef={scrollRef} resetKey={scenario.sessionId ?? scenario.id} offsetBottom={14} />
    {picked === null || onQuote === undefined ? null : (
      <SelectionBar
        picked={picked}
        onQuote={onQuote}
        onDismiss={() => {
          setPicked(null)
          window.getSelection()?.removeAllRanges()
        }}
      />
    )}
    </div>
  )
}
