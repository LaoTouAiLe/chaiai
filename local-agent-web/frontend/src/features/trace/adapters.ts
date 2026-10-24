// 会话消息 → 轨迹表（matrix 20/21）。
//
// 轨迹页签与对话页签看的是**同一份会话数据**，差别只在呈现：对话把工具调用渲染成可展开的卡片，
// 轨迹把它压平成表格并按**轮次**分组，右侧检查器再补操作详情 / 参数 / 时间三段。因此这里不新建
// 数据源，只把已经映射好的工具行摊平——两个页签不可能各自说一套。
//
// 与其它适配层同一条纪律：只搬运、不推断。契约里没有的（工具调用属于哪一轮、绑定的是哪次模型
// 调用、供应商没上报的 token）在视图里保持"未知"，宁可缺，不可编。
//
// 2026-10-16 补**轮次分组**：原来轨迹是一张没有边界的平铺表，于是"这一轮花了多少、缓存命不命中、
// 上下文用掉多少"在轨迹里答不出来——而那些数**契约里早就有**（`TurnRecordView`），
// 对话页也早就在显示（`mapTurnStats`）。这里复用同一份锚点，不另算一套边界。

import type {
  MessageView,
  ModelCallView,
  ToolRowView,
  TraceEventView,
  TraceRowView,
  TraceTurnView,
  TraceView,
  TurnStatsView,
} from '../../fixtures/types'

/** 一轮的边界与统计：由 `conversation/adapters.mapTurnStats` 给出（与对话页同一份）。 */
export interface TraceTurnStats {
  /** 该轮**最后一条消息**的 id。 */
  afterMessageId: string
  stats: TurnStatsView
  /**
   * 本轮的事件流水（工具 / 审批 / 压缩，2026-10-16）。
   *
   * 它比 `rows` 多两样东西：**审批与压缩**（它们在消息流里没有任何对应的行），以及一个
   * **确定的先后顺序**（消息流只能表达工具与回答的相对位置）。
   */
  events?: readonly TraceEventView[]
}

/**
 * 表格里"参数"与检查器 Payload 段共用的一行式写法。
 *
 * 直接沿用对话行 `summary` 的键值压法（`key=value · key=value`），不另立格式：同一份已脱敏参数
 * 在两个页签里长得一样，用户才不会以为是两批数据。
 */
export function parameterSummary(parameters: readonly { name: string; value: string }[]): string {
  return parameters.map(({ name, value }) => `${name}=${value}`).join(' · ')
}

/** 结果摘要列：预览的第一行；预览为空时返回空串，由组件决定显示"未知"。 */
export function resultSummary(resultPreview: string): string {
  const [first = ''] = resultPreview.split('\n')
  return first
}

/**
 * 一次工具调用 → 轨迹表的一行。
 *
 * `id` 用契约的 `tool_call_id`；原型 fixture 没有这个字段，就退回消息 id（同一会话内唯一）。
 */
export function toTraceRow(tool: ToolRowView, fallbackId: string): TraceRowView {
  return {
    id: tool.toolCallId ?? fallbackId,
    toolName: tool.name,
    status: tool.status,
    durationLabel: tool.durationLabel,
    target: tool.target,
    parameters: tool.parameters ?? [],
    resultPreview: tool.resultPreview ?? '',
    resultTruncated: tool.resultTruncated === true,
    resultDigest: tool.resultDigest ?? null,
    startedLabel: tool.startedLabel ?? '',
    finishedLabel: tool.finishedLabel ?? '',
    evidence: tool.evidence ?? null,
  }
}

/** 轮次标题里那行提问：取第一行并截断（长提问不该把轮次头撑开）。 */
function promptOf(message: MessageView): string {
  const text = 'text' in message && typeof message.text === 'string' ? message.text : ''
  const [first = ''] = text.trim().split('\n')
  return first.length > 80 ? `${first.slice(0, 80)}…` : first
}

/**
 * 会话消息 + 会话级模型调用（+ 每轮统计）→ 轨迹视图。
 *
 * 顺序即时间线顺序：消息流本身就是按发生次序投影的，这里排序而不重排。
 *
 * 分组口径与对话页**逐字一致**：轮次边界由契约的 `turn_usages[].user_message_id` 决定
 * （`mapTurnStats` 已经把它算成"该轮最后一条消息的 id"），所以两个页签不可能对"这是第几轮"
 * 给出不同答案。三条如实搬运：
 *
 * - **没有统计的轮** `stats: null`（页面写"未知"），不是一行 0；
 * - 统计与消息对不上（旧记录缺锚点）时**不猜归属**——那一轮仍按用户消息切出来，只是没有用量；
 * - 第一条用户消息**之前**若有工具行（系统/记忆阶段），归入一个前置分组，**不丢行**。
 */
export function traceFromMessages(
  messages: readonly MessageView[],
  calls: readonly ModelCallView[],
  turnStats: readonly TraceTurnStats[] = [],
): TraceView {
  const rows = messages.flatMap((message) =>
    message.kind === 'tool' ? [toTraceRow(message.tool, message.id)] : [],
  )

  const statsByAnchor = new Map(turnStats.map((item) => [item.afterMessageId, item.stats]))
  const eventsByAnchor = new Map(turnStats.map((item) => [item.afterMessageId, item.events ?? []]))
  const collected: { anchor: string; leadId: string; prompt: string; rows: TraceRowView[] }[] = []
  const leadingRows: TraceRowView[] = []
  let current: { anchor: string; leadId: string; prompt: string; rows: TraceRowView[] } | null = null

  for (const message of messages) {
    if (message.kind === 'user') {
      // `leadId` 记**轮首**消息：对话视图的 `data-turn-id` 挂在它上面，
      // 而 `anchor` 会被下面的循环推到轮尾（为的是对齐 `mapTurnStats`）。两者都要留。
      current = { anchor: message.id, leadId: message.id, prompt: promptOf(message), rows: [] }
      collected.push(current)
    }
    if (message.kind === 'tool') {
      const row = toTraceRow(message.tool, message.id)
      if (current === null) leadingRows.push(row)
      else current.rows.push(row)
    }
    // 锚点每次都往前挪：循环结束时它就是**这一轮的最后一条消息**，与 `mapTurnStats` 对齐。
    if (current !== null) current.anchor = message.id
  }

  const raw = [
    ...(leadingRows.length > 0
      ? [{ anchor: '__leading__', leadId: '__leading__', prompt: '', rows: leadingRows,
          leading: true }]
      : []),
    ...collected.map((turn) => ({ ...turn, leading: false })),
  ]

  // 只留"有东西可看"的轮：既没有工具行、也没有用量记录的轮（纯问候、或原型数据）不占位置——
  // 否则一个只有闲聊的会话会在轨迹里排出一串空分组。全都空时视图回落到空状态。
  // 有工具行、有用量、**或有事件**（例如只发生过一次审批/压缩的轮）就留下。
  const visible = raw.filter(
    (turn) =>
      turn.rows.length > 0
      || statsByAnchor.has(turn.anchor)
      || (eventsByAnchor.get(turn.anchor)?.length ?? 0) > 0,
  )

  // **先过滤再编号**：编号要跟着真实轮次走，中间被滤掉一轮不能让后面的跳号；
  // 前置分组固定 0（页面据此显示"会话开始"），真实轮次从 1 开始（踩过：两者都从 0 起，
  // 第一轮被显示成"会话开始"）。
  let ordinal = 0
  const turns: TraceTurnView[] = visible.map((turn) => {
    if (turn.leading) {
      return { id: turn.anchor, index: 0, prompt: '', stats: null, rows: turn.rows, events: [] }
    }
    ordinal += 1
    return {
      id: turn.anchor,
      leadId: turn.leadId,
      index: ordinal,
      prompt: turn.prompt,
      stats: statsByAnchor.get(turn.anchor) ?? null,
      rows: turn.rows,
      events: eventsByAnchor.get(turn.anchor) ?? [],
    }
  })

  return { rows, calls, turns }
}
