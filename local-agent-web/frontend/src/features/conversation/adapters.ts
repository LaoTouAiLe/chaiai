// 传输 DTO → 展示视图模型（T30；ARCHITECTURE §6.1）。
//
// 为什么要有这一层：组件只接收纯展示 view model（`fixtures/types.ts`），而传输形状由 Python 契约
// 决定（生成的 `api/schema.gen.ts`）。两者刻意不是一套东西——映射只写在这里：组件不认 DTO，
// DTO 也不为界面改形状。
//
// 这一层只做**如实搬运**：不猜、不补、不美化。拿不到的东西就是 `null`/省略，绝不为了"看起来完整"
// 编一个值出来。少数不可避免的降级（契约有、视图模型没有的取值）在下面逐条写明。

import type { components } from '../../api/schema.gen'
import type {
  ApprovalView,
  ContextMeterView,
  ErrorNoticeView,
  FileDiffView,
  MemoryRowView,
  MessageView,
  ModelCallView,
  PlanView,
  RetrievalSourceView,
  RetrievalView,
  SessionSummaryView,
  ToolCardView,
  ToolRowView,
  TurnStatsView,
} from '../../fixtures/types'
import type { StreamTail } from './streamTail'
import { toolLabel } from '../tool/display'

type Snapshot = components['schemas']['SessionSnapshot']
type TransportError = components['schemas']['ErrorView']
type ToolUse = components['schemas']['ToolUseView']
type ToolOutcome = components['schemas']['ToolOutcomeView']
type TransportSource = components['schemas']['RetrievalSourceView']
type TransportApproval = components['schemas']['ApprovalView']
type TransportDiff = components['schemas']['FileDiffView']
type TransportCall = components['schemas']['ModelCallRecordView']
type SessionState = Snapshot['session_state']

/**
 * 会话状态 → 侧栏/头部用的四态。
 *
 * 契约里有两个视图模型没有的取值，都归到 `idle`：
 * - `failed`：那一轮确实没在跑，而归成 `running` 会让界面谎称有任务在进行。失败的**依据**在
 *   快照的错误里（`errors` / 消息里的 `error` 行），不该靠侧栏一个点表达。
 * - `blocked`：本轮已结束、在等用户补充（工具被拒、循环上限、未确认副作用）。同理不能在侧栏
 *   谎称在跑。"它在等你"由对话区那条追问回答表达；给它一个专门的侧栏状态要动视图模型和
 *   54 张视觉基线，不在本次修复范围内（复审实证，2026-09-28）。
 */
const SESSION_STATE: Record<SessionState, SessionSummaryView['status']> = {
  idle: 'idle',
  running: 'running',
  awaiting_approval: 'awaiting-approval',
  blocked: 'idle',
  completed: 'completed',
  failed: 'idle',
}

/**
 * 工具状态：排队与执行分开，被拒绝与执行失败都不冒充成功。
 *
 * - `denied`（用户拒绝）与 `error` 都会显示成失败——**被拒绝不是成功**，这个方向不会误导；
 *   具体是"被拒绝"还是"执行失败"由下面 `outcome` 里的证据行与参数如实表达。
 */
const TOOL_STATUS: Record<ToolUse['status'], ToolRowView['status']> = {
  pending: 'pending',
  running: 'running',
  ok: 'succeeded',
  error: 'failed',
  denied: 'failed',
  unknown: 'uncertain',
}

/** 检索来源类型：契约用下划线，视图模型用连字符（两套拼写刻意分开，这里转一次）。 */
const SOURCE_KIND: Record<TransportSource['kind'], RetrievalSourceView['kind']> = {
  workspace_file: 'workspace-file',
  long_term_memory: 'long-term-memory',
  session_history: 'session-history',
}

/** ISO 时间（UTC）→ 本地时分。解析不出来就留空，不编时间。 */
export function timeLabel(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return ''
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return ''
  return at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

export function mapSessionState(state: SessionState): SessionSummaryView['status'] {
  return SESSION_STATE[state]
}

/** 时长：契约给毫秒；视图模型要的是**已排版**的标签（精确计时是 T34 的事）。 */
export function durationLabel(elapsedMs: number | null | undefined): string {
  if (elapsedMs === null || elapsedMs === undefined) return ''
  if (elapsedMs < 1000) return `${Math.round(elapsedMs)}ms`
  return `${(elapsedMs / 1000).toFixed(1)}s`
}

/**
 * **已运行多久**的人读标签（秒 / 分秒 / 时分），运行中的计时器与任务中心共用一处格式。
 *
 * 与 `durationLabel` 的分工：那个是"一次工具调用花了多久"（毫秒/秒精度），这个是"这一轮跑了多久"
 * （秒起步、要能读成"3 分 12 秒"）。负数按 0 处理：时钟回拨或时间戳在未来时不能显示 "-2 秒"。
 */
export function elapsedLabel(elapsedMs: number): string {
  const seconds = Math.max(0, Math.floor(elapsedMs / 1_000))
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`
  return `${Math.floor(minutes / 60)} 时 ${minutes % 60} 分`
}

/** 参数：只搬**已脱敏**的键值对（契约保证这些值不含密钥），保持契约顺序。 */
function parameterPairs(tool: ToolUse): readonly { name: string; value: string }[] {
  return Object.entries(tool.redacted_arguments ?? {}).map(([name, value]) => ({ name, value }))
}

/** 参数摘要：把上面那对键值压成一行（对话行用），结构化的那份留给轨迹检查器。 */
function argumentSummary(pairs: readonly { name: string; value: string }[]): string {
  if (pairs.length === 0) return ''
  return pairs.map(({ name, value }) => `${name}=${value}`).join(' · ')
}

/**
 * 结构化工具结果 → 卡片。
 *
 * 卡片类型由服务端给出的 `outcome.kind` 决定，**不在前端重新猜**：认不出的工具根本没有
 * `outcome`（走通用渲染器）。`文本` 一律来自服务端已脱敏、已有界的字段。
 */
function outcomeCard(tool: ToolUse, outcome: ToolOutcome): ToolCardView {
  switch (outcome.kind) {
    case 'search': {
      const groups = new Map<string, { line: number; text: string }[]>()
      for (const match of outcome.matches ?? []) {
        const matches = groups.get(match.path) ?? []
        matches.push({ line: match.line_number, text: match.text })
        groups.set(match.path, matches)
      }
      return {
        kind: 'search', shape: 'matches', paths: [],
        groups: [...groups].map(([path, matches]) => ({ path, matches })),
        total: outcome.total ?? (outcome.matches ?? []).length, totalUnit: 'matches', truncated: outcome.truncated,
      }
    }
    case 'read': {
      const body = outcome.text ?? ''
      const lines = body === '' ? [] : body.split('\n').map((text, index) => ({ number: index + 1, text }))
      return {
        kind: 'read',
        path: outcome.target ?? '读取结果',
        lines,
        totalLines: outcome.total ?? lines.length,
        language: '',
        truncated: outcome.truncated,
      }
    }
    case 'list': {
      const paths = outcome.paths ?? []
      return {
        kind: 'search',
        shape: 'paths',
        groups: [],
        paths,
        total: outcome.total ?? paths.length,
        truncated: outcome.truncated,
      }
    }
    case 'shell':
      return {
        kind: 'terminal',
        // 命令本身是已脱敏参数；cwd 与退出码来自结构化结果，不从自然语言里解析。
        command: tool.redacted_arguments?.command ?? '命令未记录',
        cwd: outcome.target ?? '',
        exitCode: outcome.exit_code ?? null,
        signal: null,
        // 兼容字段：结构化流缺失时仍要有正文可渲染（原型 fixtures 走这一支）。
        output: outcome.stdout ?? '',
        stdout: outcome.stdout ?? undefined,
        stderr: outcome.stderr ?? undefined,
        timedOut: outcome.timed_out ?? undefined,
        collapsedLines: 0,
      }
    case 'edit':
    case 'write':
    case 'delete': {
      // 服务端解析出的结构化 diff 优先：页面直接渲染 hunk 与行号，不再解析任何文本。
      // 没有结构化 diff（认不出的形状）才退回有界文本卡。
      if (outcome.diff !== null && outcome.diff !== undefined) {
        return { kind: 'file-mutation', diff: mapDiff(outcome.diff) }
      }
      return {
        kind: 'result',
        target: outcome.target ?? '',
        summary: outcome.summary ?? '',
        body: outcome.text ?? '',
        truncated: outcome.truncated,
      }
    }
  }
}

/** 结构化文件改动：契约用下划线，视图模型用驼峰（两套拼写刻意分开，这里转一次）。 */
export function mapDiff(diff: TransportDiff): FileDiffView {
  return {
    path: diff.path,
    additions: diff.additions,
    deletions: diff.deletions,
    hunks: (diff.hunks ?? []).map((hunk) => ({
      header: hunk.header,
      lines: (hunk.lines ?? []).map((line) => ({
        kind: line.kind,
        oldLine: line.old_line ?? null,
        newLine: line.new_line ?? null,
        text: line.text,
      })),
    })),
  }
}

/**
 * 审批 DTO → 审批卡视图模型。
 *
 * 两处如实搬运：
 * - `impact` 留空：后端只给一段 `impact_summary`，**没有结构化的影响清单**。为了"看起来完整"
 *   去拆那句话就等于前端自己解析自然语言，那是禁止的；
 * - `parameters` 直接来自已脱敏的参数（界面永远看不到原文）。
 */
export function mapApproval(approval: TransportApproval): ApprovalView {
  return {
    approvalId: approval.approval_id,
    action: approval.tool_name,
    summary: approval.impact_summary,
    target: approval.target ?? '',
    outsideWorkspace: approval.outside_workspace,
    confirmationStage: approval.confirmation_stage,
    impact: [],
    risk: approval.risk,
    state: approval.state,
    note: approval.note ?? null,
    parameters: Object.entries(approval.redacted_arguments ?? {}).map(([name, value]) => ({ name, value })),
  }
}

export function mapTool(tool: ToolUse): ToolRowView {
  const preview = tool.output_preview ?? ''
  const outcome = tool.outcome ?? null
  const parameters = parameterPairs(tool)
  return {
    name: tool.tool_name,
    summary: argumentSummary(parameters),
    status: TOOL_STATUS[tool.status],
    durationLabel: durationLabel(tool.elapsed_ms),
    // 归一化目标来自结构化结果；没有结果时保持 `null`，不从参数里猜它是哪个字段。
    target: outcome?.target ?? null,
    // 展开态是**界面状态**，由容器注入；适配层给"折叠"（默认紧凑）。
    expanded: false,
    // 轨迹视图与对话行看的是同一次调用：这里把契约里有、卡片用不到的字段如实带上，
    // 免得轨迹再去别处拼一份可能对不上的数据。
    toolCallId: tool.tool_call_id,
    parameters,
    resultPreview: preview,
    resultTruncated: tool.truncated === true,
    resultDigest: null,
    startedLabel: timeLabel(tool.started_at),
    finishedLabel: timeLabel(tool.finished_at),
    evidence:
      tool.evidence === null || tool.evidence === undefined
        ? null
        : {
          evidenceType: '',
          summary: tool.evidence.summary,
          verified: tool.evidence.verified,
        },
    card:
      outcome === null
        ? {
          kind: 'generic',
          headline: toolLabel(tool.tool_name),
          detail: tool.truncated === true && preview !== '' ? `${preview}\n（输出已截断）` : preview,
        }
        : outcomeCard(tool, outcome),
  }
}

/** 计划条：没有计划就是 `null`，不用空计划占位（契约已经这样返回）。 */
export function mapPlan(plan: Snapshot['plan']): PlanView | null {
  if (plan === null || plan === undefined || (plan.steps ?? []).length === 0) return null
  return {
    goal: plan.goal,
    steps: (plan.steps ?? []).map((step) => ({
      id: step.step_id,
      title: step.title,
      status: step.status,
    })),
  }
}

function mapSource(source: TransportSource): RetrievalSourceView {
  return {
    kind: SOURCE_KIND[source.kind],
    reference: source.reference,
    usage: source.usage,
  }
}

/**
 * 检索与长期记忆行。
 *
 * `failed`（看不了）与 `miss`（查了没有）分开：契约的 `retrieval_outcome` 已经区分，
 * 这里只搬运，不合并。失败时的说明只有一句（契约的 `retrieval_note`），放进 `reason`
 * 用于表头标注，`note` 留空避免同一句话显示两遍。
 */
export function mapMemory(memory: Snapshot['memory']): MemoryRowView | null {
  if (memory === null || memory === undefined) return null
  const note = memory.retrieval_note ?? ''
  let retrieval: RetrievalView
  if (memory.retrieval_outcome === 'hit') {
    retrieval = {
      outcome: 'hit',
      sources: (memory.sources ?? []).map(mapSource),
      note,
    }
  } else if (memory.retrieval_outcome === 'failed') {
    retrieval = { outcome: 'failed', reason: note, note: '' }
  } else {
    retrieval = { outcome: 'miss', note }
  }
  const write =
    memory.write_outcome === null || memory.write_outcome === undefined
      ? null
      : { outcome: memory.write_outcome, detail: memory.write_detail ?? '' }
  return { retrieval, write }
}

/** 仅合并同一轮完全相同的错误提示；原始轨迹和诊断记录保留在服务端。 */
export function collapseDuplicateErrors(messages: MessageView[]): MessageView[] {
  const seen = new Set<string>()
  return messages.filter((message) => {
    if (message.kind === 'user') seen.clear()
    if (message.kind !== 'error') return true
    const key = JSON.stringify([message.notice.code, message.notice.message, message.notice.hint])
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function mapError(error: TransportError): ErrorNoticeView {
  // 与契约近乎同构（字段名按视图模型的拼写转一次），没有需要判断的地方。
  return {
    code: error.code,
    category: error.category,
    severity: error.severity,
    title: error.title,
    message: error.message,
    hint: error.hint,
    retryable: error.retryable,
    blockedReason: error.blocked_reason ?? null,
    diagnosticId: error.diagnostic_id,
  }
}

/** 快照里是否已经有一条**完整**的助手消息（与 `streamTail.reconcile` 同一条规则）。 */
function hasCompleteAssistant(snapshot: Snapshot, messageId: string): boolean {
  return (snapshot.messages ?? []).some(
    (message) => message.message_id === messageId && message.kind === 'assistant' && message.complete,
  )
}

/**
 * 快照消息 + 临时尾部 → 消息流。
 *
 * 尾部只在"快照里还没有这条完整消息"时追加：否则同一段回答会显示两遍
 * （T30 停止条件之一，规则与 `streamTail.reconcile` 同源）。
 */
export function mapMessages(snapshot: Snapshot, tail: StreamTail | null = null): MessageView[] {
  const mapped: MessageView[] = []
  for (const message of snapshot.messages ?? []) {
    switch (message.kind) {
      case 'user':
        mapped.push({
          kind: 'user',
          id: message.message_id,
          text: message.text ?? '',
          at: timeLabel(message.created_at),
        })
        break
      case 'assistant':
        mapped.push({
          kind: 'assistant',
          id: message.message_id,
          text: !message.complete && tail?.messageId === message.message_id && tail.text !== ''
            ? tail.text
            : message.text ?? '',
          at: timeLabel(message.created_at),
          reasoning: !message.complete && tail?.messageId === message.message_id
            ? tail.reasoning ?? message.reasoning ?? '' : message.reasoning ?? '',
          reasoningTruncated: !message.complete && tail?.messageId === message.message_id
            ? tail.reasoningTruncated ?? message.reasoning_truncated : message.reasoning_truncated,
          streaming: !message.complete,
          reasoningRunning: !message.complete && (tail?.messageId === message.message_id
            ? tail.channel ?? message.stream_channel : message.stream_channel) === 'reasoning',
        })
        break
      case 'tool':
        mapped.push({ kind: 'tool', id: message.message_id, tool: mapTool(message.tool) })
        break
      case 'error':
        mapped.push({ kind: 'error', id: message.message_id, notice: mapError(message.error) })
        break
      case 'memory': {
        const memory = mapMemory(message.memory)
        if (memory !== null) {
          mapped.push({ kind: 'memory', id: message.message_id, memory })
        }
        break
      }
      default:
        break
    }
  }
  if (tail !== null && (tail.text !== '' || (tail.reasoning ?? '') !== '') &&
    !(snapshot.messages ?? []).some((message) => message.message_id === tail.messageId) &&
    !hasCompleteAssistant(snapshot, tail.messageId)) {
    mapped.push({ kind: 'assistant', id: tail.messageId, text: tail.text, at: '',
      reasoning: tail.reasoning, reasoningTruncated: tail.reasoningTruncated,
      streaming: true, reasoningRunning: tail.channel === 'reasoning' })
  }
  return mapped
}

/** 上下文计量器：契约的字段名换成视图模型的拼写；缺失保持 `null`（不估算）。 */
export function mapContext(snapshot: Snapshot): ContextMeterView {
  const context = snapshot.context
  return {
    usedTokens: context?.used_tokens ?? null,
    windowTokens: context?.window_tokens ?? null,
    scope: context?.scope ?? 'unknown',
  }
}

/**
 * 调用用途 → 页面说法。
 *
 * 契约里的 `purpose` 已经是**产品级词表**（架构 §15.4：不得泄露内部节点名），这里只把它译成人话，
 * 出现词表外的取值时显示“模型调用”，不把内部用途名写进页面。
 */
const PURPOSE_LABEL: Record<string, string> = {
  chat: '闲聊回答',
  main_decision: '主模型处理',
  tool_decision: '工具决策',
  final_answer: '最终回答',
  summarize: '上下文摘要',
  planning: '计划拆步',
  reflection: '结果反思',
  memory_intent: '记忆意图',
  memory_extraction: '任务记忆提取',
  retrieval: '检索决策',
  routing: '意图路由',
}

export function modelPurposeLabel(purpose: string): string {
  return PURPOSE_LABEL[purpose] ?? '模型调用'
}

/**
 * 一次模型调用 → 展示形状。
 *
 * 导出的原因：轨迹页签的 Schema 段要按**会话级**列出模型调用（契约没有"工具调用属于哪次模型调用"
 * 的绑定字段），它用的是同一份映射，不能另起一套。
 */
export function mapCall(call: TransportCall): ModelCallView {
  return {
    id: call.model_call_id,
    purpose: modelPurposeLabel(call.purpose),
    modelLabel: call.model_id,
    inputTokens: call.actual_uncached_input_tokens ?? null,
    outputTokens: call.actual_output_tokens ?? null,
    elapsedLabel: durationLabel(call.model_elapsed_ms),
    // 供应商/服务端没记录就是 `null`（页面写"未知"），不编一个指纹出来。
    schemaFingerprint: null,
  }
}

/**
 * 每轮统计行：整轮汇总 + 可展开的逐次调用明细（架构 §15.6）。
 *
 * 三处刻意的"如实搬运"：
 *
 * - 汇总**不在这里算**：组件会拿 `calls` 确定性相加（`Conversation.aggregate`），这里的
 *   `inputTokens` / `outputTokens` 只在没有明细时兜底；估算输入（`estimated_prompt_tokens`）
 *   因此只在"旧 Checkpoint 没有记录"时出现，并用 `estimated` 标记，不冒充供应商实际量；
 * - 供应商没上报的字段是 `null`（页面显示"未知"），不是 0——"未上报"与"上报为零"是两回事；
 * - `usage` 为空（这一轮还没开始）时整块统计行不渲染，而不是显示一行 0。
 */
function mapUsage(
  usage: components['schemas']['TurnRecordView'],
  calls: readonly TransportCall[],
  steps: number,
): TurnStatsView {
  const recorded = usage.usage_recorded
  const output = usage.actual_output_tokens ?? null
  const modelElapsedMs = usage.model_elapsed_ms ?? null
  return {
    // 一次提交到终态就是一轮（§15.6："本轮"即一次用户提交）。
    turns: 1,
    steps,
    cacheHitRatio: usage.cache_hit_ratio ?? null,
    // 有记录就只显示**实际量**（没上报就是"未知"）；没有记录（旧 Checkpoint）才退回估算输入，
    // 而且只在那一支用 `estimated` 标记，估算值不会被当成供应商实际量。
    inputTokens: recorded ? (usage.actual_uncached_input_tokens ?? null) : (usage.estimated_prompt_tokens ?? null),
    outputTokens: output,
    elapsedLabel: durationLabel(usage.turn_elapsed_ms),
    // 耗时三项与首 token 直接来自契约，不做二次计算；缺失留空串 → 页面显示"未知"。
    modelElapsedLabel: durationLabel(usage.model_elapsed_ms),
    toolElapsedLabel: durationLabel(usage.tool_elapsed_ms),
    firstTokenLabel: durationLabel(usage.time_to_first_token_ms),
    // 吞吐只在两个数都是**实际量**时才算：缺一个就不猜（§15.6 "未上报 ≠ 0"）。
    throughput:
      output !== null && modelElapsedMs !== null && modelElapsedMs > 0
        ? Math.round(output / (modelElapsedMs / 1000))
        : null,
    cacheReported: usage.cache_metrics_reported ?? false,
    estimated: !recorded,
    calls: calls.map(mapCall),
  }
}

export function mapStats(snapshot: Snapshot): TurnStatsView | null {
  const usage = snapshot.usage
  if (usage === null || usage === undefined) return null
  return mapUsage(usage, snapshot.model_calls ?? [], (snapshot.plan?.steps ?? []).length)
}

/** 按持久用户消息锚点把各轮统计放回对话流；旧记录缺锚点时不猜归属。 */
export function mapTurnStats(snapshot: Snapshot, messages: readonly MessageView[], hideCurrent = false) {
  const turns = snapshot.turn_usages ?? []
  const userPositions = messages.flatMap((message, index) =>
    message.kind === 'user' ? [index] : [],
  )
  return turns.flatMap((turn) => {
    if (hideCurrent && turn.usage.run_id === snapshot.current_task?.run_id) return []
    const start = messages.findIndex((message) => message.id === turn.user_message_id && message.kind === 'user')
    if (start < 0) return []
    const next = userPositions.find((position) => position > start) ?? messages.length
    const end = messages[next - 1]
    if (end === undefined) return []
    return [{
      afterMessageId: end.id,
      stats: mapUsage(turn.usage, turn.model_calls ?? [], turn.usage.run_id === snapshot.usage?.run_id
        ? (snapshot.plan?.steps ?? []).length : 0),
    }]
  })
}

/** 会话摘要（头部与输入区用）：标题、工作区显示名、状态、当前模型标签。 */
export function mapSession(snapshot: Snapshot, modelLabel: string): SessionSummaryView {
  return {
    title: snapshot.title,
    workspace: snapshot.workspace?.display_name ?? null,
    status: mapSessionState(snapshot.session_state),
    modelLabel,
  }
}

/** 某个会话下一轮的模型标签；跟随默认时读取连接列表中的默认项，不猜运行中那轮。 */
export function modelLabelFor(
  snapshot: Snapshot,
  connections: readonly { connection_id: string; model_id: string; is_default?: boolean }[],
): string {
  const selected = snapshot.model_selection?.connection_id
    ?? connections.find((item) => item.is_default === true)?.connection_id
    ?? null
  if (selected === null) return ''
  return connections.find((item) => item.connection_id === selected)?.model_id ?? ''
}
