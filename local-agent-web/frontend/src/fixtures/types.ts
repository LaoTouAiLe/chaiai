/**
 * Pure display view models for the static T26 prototypes.
 *
 * These are **view models, not transport DTOs**: they describe what a component
 * renders, and deliberately do not mirror Graph State, node names or the future
 * application API (V0_4_DEVELOPMENT_PLAN §6.1). T29A/T30 add the adapter that
 * maps real DTOs onto these shapes.
 *
 * **The Python DTO is the only business contract** (`local_agent/application/models.py`,
 * `errors.py`; ARCHITECTURE §15.1). Where string literals overlap they match on
 * purpose, but these are not a second transport enum and must not grow like one.
 * Known divergences to resolve in T29A/T30 when generated types arrive:
 *
 * - key status: `session-only` here vs `session_only` in Python
 * - session state: `awaiting-approval` here vs `awaiting_approval` (Python also has `failed`)
 * - tool status: `succeeded/failed` here vs `ok/error/denied/pending/running` in Python
 * - `SESSION_BUSY.retryable`: `false` in this prototype vs `true` in the contract
 *   (§15.5 means "retryable once the current run ends"; the page still shows no button)
 */

export interface DiffLineView {
  kind: 'context' | 'add' | 'del'
  /** Line number in the old file, or null for an added line. */
  oldLine: number | null
  /** Line number in the new file, or null for a deleted line. */
  newLine: number | null
  text: string
}

export interface DiffHunkView {
  /** Unified-diff hunk header, e.g. `@@ -12,7 +12,9 @@`. */
  header: string
  lines: DiffLineView[]
}

export interface FileDiffView {
  /** Workspace-relative path. Never an absolute private path. */
  path: string
  additions: number
  deletions: number
  hunks: DiffHunkView[]
}

export interface ReadLineView {
  /** 1-based file line number; a windowed read starts above 1. */
  number: number
  text: string
}

export interface SearchGroupView {
  path: string
  matches: ReadonlyArray<{ line: number; text: string }>
}

/**
 * What a tool call produced, as the card that renders it.
 *
 * The union is keyed by `kind` so a card can never be rendered from a payload it
 * does not understand: an unknown tool falls back to `generic`, which is the
 * only shape allowed to show arbitrary text.
 */
export type ToolCardView =
  | { kind: 'file-mutation'; diff: FileDiffView }
  | {
    kind: 'read'
    path: string
    lines: readonly ReadLineView[]
    /** Total lines in the file, so a windowed read can say "showing N of M". */
    totalLines: number
    language: string
    /**
     * File line number of `lines[0]` (1-based) — from the contract's `first_line`
     * (§19.3). Without it the card can only number the window from 1, which
     * silently disagrees with the file. Optional so prototype fixtures (no
     * windowed reads) are unaffected.
     */
    firstLine?: number
    /** Body was capped for display; optional so prototype fixtures are unaffected. */
    truncated?: boolean
  }
  | {
    kind: 'search'
    /** `matches` groups by file; `paths` is a flat path list. */
    shape: 'matches' | 'paths'
    groups: readonly SearchGroupView[]
    paths: readonly string[]
    total: number
    /** Content-search totals count matching lines, not files. */
    totalUnit?: 'matches'
    truncated: boolean
  }
  | {
    kind: 'terminal'
    command: string
    cwd: string
    /** Non-zero exits and signals are separate facts; never merged into text. */
    exitCode: number | null
    signal: string | null
    output: string
    /** How many lines the height cap collapsed, 0 when nothing was collapsed. */
    collapsedLines: number
    /**
     * Structured streams for a real shell call. Omitted means `output` is the
     * only body (prototype fixtures); present means stdout/stderr are shown
     * separately instead of being concatenated into one blob (plan §6.7).
     */
    stdout?: string
    stderr?: string
    timedOut?: boolean
    /** 命令仍在后台运行（用户点了"转后台"，2026-10-21）：不是超时，进程还活着。 */
    background?: boolean
    /**
     * The server clipped the streams for display (§15.8). Optional so prototype
     * fixtures are unaffected; when true the card must say the body is a bounded
     * preview and how to get the whole thing (§19.3).
     */
    truncated?: boolean
  }
  | {
    kind: 'result'
    /** Normalised target: a workspace-relative path, never an absolute private one. */
    target: string
    /** One-line outcome summary (change counts, byte size, deletion counts). */
    summary: string
    /** Bounded body: a diff or file note; empty when the result has no body. */
    body: string
    truncated: boolean
  }
  | {
    /**
     * 网页检索 / 读取（2026-10-26）。与 `result` 分开：正文不是等宽代码，截断后的续读方式是
     * 按 `start_char` 继续取，而不是像文件那样按行区间重读。
     */
    kind: 'web'
    /** 搜索词或页面标题；投影层给不出时为空串。 */
    target: string
    /** 一行事实：`11 条来源 · 后端 deepseek`、`3200 字符 · 已跟随重定向`。 */
    summary: string
    /** 有界正文：读取到的正文，或前几条命中标题（每行一条，末尾可带说明）。 */
    body: string
    truncated: boolean
  }
  | {
    kind: 'todo'
    items: readonly { text: string; status: 'completed' | 'in_progress' | 'pending' }[]
  }
  | {
    kind: 'delegation'
    /** 每个子 Agent 的结局（§18.7）：状态、工具次数、token 预留、证据条数与失败原因。 */
    children: readonly DelegationChildView[]
    /** 父图核对过的原文报告（带真实文件行号）；未完成时里面是原因说明。 */
    report: string
    truncated: boolean
  }
  | { kind: 'generic'; headline: string; detail: string }

/**
 * 一次自主委派里一个子 Agent 的结局（v1.0 §18.7）。
 *
 * 字段借用参照实现的任务面板（成员 / 状态 / 工具调用次数 / 证据条数）。
 * **刻意不含"预留 token"**（2026-10-13 裁定）：那是切出来的上限、同一份配置下每路几乎一样，
 * 摆在对话卡里只会被读成"这一路花了多少"。实际花费看用量区（§15.7 要求预留与实际分开写）。
 */
export interface DelegationChildView {
  scope: string
  status: 'completed' | 'failed'
  toolCalls: number
  modelCalls: number
  evidenceCount: number
  error: string | null
}

/**
 * Mechanical check produced by the reflection layer for one tool call.
 *
 * `verified` is a computed fact (hash changed, exit code read, diff present),
 * never the model's opinion of its own work (ARCHITECTURE §4.3, plan §6.7).
 */
export interface ToolEvidenceView {
  evidenceType: string
  /** Safe, already-redacted one-line summary. */
  summary: string
  verified: boolean
}

export interface WebSourceView {
  title: string
  url: string
  kind: 'search' | 'read'
  retrievedAt?: string | null
  sourceId?: string
  cited?: boolean
  publishedAt?: string | null
  provider?: string | null
}

export interface ToolRowView {
  /** Tool name as the user sees it. */
  name: string
  /** Short human summary of what the call is doing. */
  summary: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'uncertain'
  /** Pre-formatted duration; measuring timings is T34's job. */
  durationLabel: string
  /**
   * 这次调用的开始时刻（ISO）。运行中的行据此算"已跑多久"；
   * 省略时页面只显示已排版好的 `durationLabel`（原型 fixture 走这一支）。
   */
  startedAt?: string
  target: string | null
  expanded: boolean
  card: ToolCardView
  /** Null/omitted while the call has not been settled by the reflection layer. */
  evidence?: ToolEvidenceView | null
  /**
   * Stable id of this call in the contract (`tool_call_id`).
   *
   * Optional so the prototype fixtures keep working; the trace view keys rows by
   * it and the real adapter always fills it.
   */
  toolCallId?: string
  /**
   * The redacted arguments as name/value pairs, in contract order.
   *
   * `summary` flattens the same data into one display string; the trace inspector
   * needs the structured pairs ("Payload"), so both are carried rather than one
   * being re-parsed out of the other.
   */
  parameters?: readonly { name: string; value: string }[]
  /** Bounded result preview as reported by the server; dropped from the card when an outcome exists. */
  resultPreview?: string
  /** Whether `resultPreview` was capped for transport. */
  resultTruncated?: boolean
  /** Stable digest of the full result, when the server computed one. */
  resultDigest?: string | null
  /** Local wall-clock labels; empty when the contract has no parsable timestamp. */
  startedLabel?: string
  finishedLabel?: string
  webSources?: readonly WebSourceView[]
}

export type ErrorCategory =
  | 'configuration'
  | 'model'
  | 'tool'
  | 'security'
  | 'approval'
  | 'recovery'
  | 'system'

export interface ErrorNoticeView {
  /** Stable, user-facing code (e.g. `MODEL_AUTH_FAILED`). */
  code: string
  category: ErrorCategory
  severity: 'error' | 'warning'
  title: string
  /** What happened, in plain language. Must never include raw provider payloads. */
  message: string
  /** What the user can do next. Always present. */
  hint: string
  retryable: boolean
  /**
   * Why no retry button is offered. Required in spirit when `retryable` is false:
   * one hard-coded sentence ("先处理配置") is wrong for codes that are not a
   * configuration problem (`SESSION_BUSY`, `RECOVERY_REQUIRED`).
   */
  blockedReason: string | null
  /** Safe correlation id for local logs; never a key or a private path. */
  diagnosticId: string
}

export interface PlanStepView {
  id: string
  title: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'blocked'
}

export interface PlanView {
  goal: string
  steps: PlanStepView[]
}

export interface TurnStatsView {
  /** Number of user turns and plan steps in this run. */
  turns: number
  steps: number
  /** Actual provider numbers when reported, `null` when unknown. */
  cacheHitRatio: number | null
  inputTokens: number | null
  outputTokens: number | null
  elapsedLabel: string
  /**
   * 模型耗时 / 工具耗时 / 首 token / 输出吞吐 / 缓存是否上报（2026-09-30）。
   *
   * 这些值**契约里本来就有**（`TurnRecordView.model_elapsed_ms` / `tool_elapsed_ms` /
   * `time_to_first_token_ms` / `cache_metrics_reported`），只是视图模型此前没搬过来，页面因此
   * 只能显示一个笼统的整轮耗时。
   *
   * 与 `elapsedLabel` 同一口径：拿不到就是空串，页面显示"未知"，不伪造 0。
   *
   * **可选**的原因与 `ModelCallView.schemaFingerprint` 相同：原型 fixture 早于这几个字段存在，
   * 而原型本来就没有计时数据——给它编一组数字会变成"看起来像实测"。真会话由 `mapUsage` 一律填。
   */
  modelElapsedLabel?: string
  toolElapsedLabel?: string
  firstTokenLabel?: string
  /** 输出吞吐（token/秒），由实际输出 token ÷ 模型耗时算得；分母缺失时为 `null`。 */
  throughput?: number | null
  /** 供应商是否上报了缓存字段。`false` 时页面说"未上报"，不显示 0%。 */
  cacheReported?: boolean
  /** `true` when the token numbers are local estimates rather than provider facts. */
  estimated: boolean
  /** One entry per model call in this turn, for the expanded breakdown. */
  calls: readonly ModelCallView[]
  /**
   * 上下文占用（2026-10-16 补）：`context_used_tokens` / `context_window_tokens`。
   *
   * 拿不到时是 `null`——轨迹页因此显示"未知"，不会画一个假的占用率。**注意这不是"曲线"**：
   * 曲线要逐轮采样，契约里没有那个数据。
   */
  contextUsedTokens?: number | null
  contextWindowTokens?: number | null
  /** 这一轮的终态（`completed` / `blocked` / `cancelled` / `failed`…；§15.6）。 */
  finalState?: string | null
}

export interface ModelCallView {
  id: string
  purpose: string
  modelLabel: string
  inputTokens: number | null
  outputTokens: number | null
  elapsedLabel: string
  /**
   * Fingerprint of the tool schema sent with this call (`tools_schema_fingerprint`).
   *
   * The trace inspector's "Schema" section is the only reader. Optional because the
   * prototype fixtures predate it and the provider may not have reported one; the
   * page then prints 未知 rather than inventing a hash.
   */
  schemaFingerprint?: string | null
  /**
   * 逐次调用的**缓存账目与推理 token、首字延迟**（2026-10-16 从轨迹页的需求补上）。
   *
   * 契约里本来就有（`ModelCallRecordView.actual_cache_read_tokens` / `actual_cache_write_tokens` /
   * `actual_reasoning_tokens` / `time_to_first_token_ms`），只是视图模型没搬——于是轨迹页只能看到
   * 笼统的输入/输出 token，"这次调用吃掉了多少前缀缓存"完全看不见。
   *
   * 与全仓同一条口径：**供应商没上报就是 `null` / 空串**，页面写"未上报"，绝不写 0。
   * 可选的另一个原因：原型 fixture 早于这几个字段，给它编数字会变成"看起来像实测"。
   */
  cacheReadTokens?: number | null
  cacheWriteTokens?: number | null
  reasoningTokens?: number | null
  /** 首字延迟（已格式化，如 `1.2s`）；空串 = 未知。 */
  firstTokenLabel?: string
  /**
   * 原始时刻（ISO 字符串，2026-10-16）：时间轴按它们画 span——首字把一段调用切成
   * "等首字"与"解码"两截（TTFT 拆分）。缺失是空串：页面就不画那一段，不拿别的时刻凑。
   */
  startedAt?: string
  firstTokenAt?: string
  finishedAt?: string
}

/**
 * 轨迹里的一条事件（2026-10-16；契约的 `TraceEventView`）。
 *
 * `kind` 是闭集：**工具 / 审批 / 压缩**。它们此前在轨迹里完全看不见——而"为什么这一轮慢、
 * 上下文为什么变小、是谁批准的"答案正在这里。时刻是 ISO 字符串（可能缺失），格式化是页面的事。
 */
export interface TraceEventView {
  kind: 'tool' | 'approval' | 'compaction'
  id: string
  /** 主时刻：工具=开始、审批=请求、压缩=发生；缺失为空串。 */
  at: string
  /** 结束时刻：工具=完成、审批=决定；缺失为空串。 */
  finishedAt: string
  label: string
  /** 工具状态（`ok`/`denied`/`failed`）或审批结论（`approved`/`denied`）；契约没给是 null。 */
  status: string | null
  /** 审批等待毫秒：由契约的"请求→决定"给，不是页面自己减出来的。 */
  waitMs: number | null
  /** 压缩折叠掉的消息条数与前后**估算** token。 */
  foldedMessages: number | null
  tokensBefore: number | null
  tokensAfter: number | null
}

/**
 * One row of the trace table (matrix 20/21): a single tool call, flattened.
 *
 * Separate from `ToolRowView` on purpose: the conversation row renders a `card`
 * (a discriminated union), while a table row and its inspector need flat,
 * independently print-or-say-unknown fields. Both views are built from the same
 * tool call so the two tabs of one session can never disagree.
 */
export interface TraceRowView {
  /** Row key: the contract's `tool_call_id`. */
  id: string
  /** Type column: the tool name as the user sees it. */
  toolName: string
  status: ToolRowView['status']
  /** Pre-formatted duration; empty when the contract did not report one. */
  durationLabel: string
  target: string | null
  /** Parameters column/Payload section: redacted name/value pairs, contract order. */
  parameters: readonly { name: string; value: string }[]
  /** Result column/Result section: bounded preview, empty when none was recorded. */
  resultPreview: string
  resultTruncated: boolean
  resultDigest: string | null
  /** Local wall-clock labels; empty when unparsable — the page then says 未知. */
  startedLabel: string
  finishedLabel: string
  /** Mechanical evidence, when the reflection layer settled this call. */
  evidence: ToolEvidenceView | null
}

/**
 * The trace tab's data: tool calls in timeline order plus the session-level model
 * calls.
 *
 * The model calls are session-level because the contract has **no field binding a
 * tool call to the model call that produced it**; the inspector's Schema section
 * therefore lists them per session and says so instead of guessing an attribution.
 */
/**
 * 轨迹页的一轮（2026-10-16 新增）。
 *
 * 为什么要有它：轨迹原来是一张**没有轮次边界**的平铺表，于是"这一轮花了多少、缓存命不命中、
 * 上下文用掉多少"在轨迹里根本答不出来——而那些数**契约里早就有**（`TurnRecordView`）。
 * 分组锚点用契约的 `turn_usages[].user_message_id`（`mapTurnStats` 已经这么做了，与对话页同一份）。
 */
export interface TraceTurnView {
  /** 轮次键：该轮**最后一条消息**的 id（与 `mapTurnStats` 的 `afterMessageId` 一致）。 */
  id: string
  /**
   * 该轮**第一条消息**的 id（2026-10-22）。
   *
   * 对话视图的 `data-turn-id` 挂的是轮首消息，而 `id` 是轮尾消息——两者不同。
   * 从「步骤」跳回对话里某一轮时必须用这个值，否则 `querySelector` 永远匹配不到。
   * 可选：原型 fixture 不填，页面退回 `id`。
   */
  leadId?: string
  /** 第几轮（1 基，仅用于显示）。 */
  index: number
  /** 该轮的提问（截断；空串 = 契约没给）。 */
  prompt: string
  /**
   * 该轮的用量聚合；`null` = **契约没有这一轮的记录**（旧 Checkpoint 或原型 fixture）
   * ——页面显示"未知"，不显示一行 0。
   */
  stats: TurnStatsView | null
  /** 这一轮里的工具行（顺序即时间线顺序）。 */
  rows: readonly TraceRowView[]
  /**
   * 这一轮的事件时间线（工具 / 审批 / 压缩，2026-10-16）。
   *
   * 与 `rows` 的分工：`rows` 是"工具调用表"（点开看参数与结果），`events` 是**发生顺序的流水**
   * ——它是唯一的顺序真源（审批与压缩在消息流里根本没有对应的行）。为空 = 契约没有这一轮的记录
   * （旧 Checkpoint），页面就不画，不编。
   */
  events: readonly TraceEventView[]
}

export interface TraceView {
  rows: readonly TraceRowView[]
  calls: readonly ModelCallView[]
  /**
   * 按轮次分组（2026-10-16）。**为空时页面回落到平铺表**——原型 fixture 与旧快照因此照常工作。
   */
  turns: readonly TraceTurnView[]
}

export interface SessionSummaryView {
  title: string
  /** Null while no workspace is selected: the shell then shows no breadcrumb. */
  workspace: string | null
  status: 'idle' | 'running' | 'awaiting-approval' | 'awaiting-input' | 'completed'
  modelLabel: string
  /**
   * Whether this session's next answer arrives as increments (`true`) or in one
   * piece (`false`) — straight from the contract's `answer_streaming` (§19.4).
   *
   * Optional so prototype fixtures are unaffected: absent means "the server did
   * not say", and the page must then show **no** streaming claim at all rather
   * than guess from whether increments happen to be arriving.
   */
  answerStreaming?: boolean
}

export interface ContextMeterView {
  usedTokens: number | null
  windowTokens: number | null
  /** Whether the numbers describe the in-flight request or the previous one. */
  scope: 'current' | 'previous' | 'unknown'
  compression?: 'running' | 'completed' | 'failed' | null
}

/**
 * A request for authorisation that took over the composer.
 *
 * `state` is what the card renders; the front-end button is never the authority
 * that executes anything (ARCHITECTURE §15.8).
 */
export interface ApprovalView {
  approvalId: string
  action: string
  summary: string
  /** Normalised target as the security layer resolved it. */
  target: string
  outsideWorkspace?: boolean
  confirmationStage?: 1 | 2
  impact: readonly string[]
  risk: 'normal' | 'high'
  state: 'pending' | 'executing' | 'approved' | 'rejected' | 'expired' | 'interrupted'
  /** Shown for a refused or stale request instead of offering the buttons again. */
  note: string | null
  /**
   * The redacted arguments this decision applies to, in contract order.
   *
   * Omitted by the prototype fixtures (they show only the normalised target);
   * the real adapter always fills it from `redacted_arguments`, so the card can
   * show *what* is being authorised without the page ever seeing raw values.
   */
  parameters?: readonly { name: string; value: string }[]
}

/**
 * 一个"等用户回答"的提问（2026-10-28，ARCHITECTURE §6.14）。
 *
 * `state` 决定卡片形态：只有 `pending` 给输入框与按钮；其余只报结果（与审批同一口径：
 * 一次性的东西一旦有结局就不再提供第二次机会）。
 */
export interface QuestionItemView {
  index: number
  question: string
  /** 最多 3 个候选；空 = 只能自由输入。 */
  options: readonly string[]
  allowFreeText: boolean
  /** 用户对这一题的答复（已脱敏）；未答或跳过时为 `null`。 */
  answer: string | null
  skipped: boolean
}

export interface QuestionView {
  questionId: string
  /** 第一题的正文（单题兼容；多题时以 `items` 为准）。 */
  question: string
  /** 整组题（1–5 题）：页面据此做"1/3"式翻页。 */
  items: readonly QuestionItemView[]
  state: 'pending' | 'answered' | 'skipped' | 'expired'
  /** 单题兼容的答复；多题时为 `null`，逐题看 `items[].answer`。 */
  answer: string | null
  /** 终态说明（"你跳过了…" / "该提问已失效…"）；待决时为 `null`。 */
  note: string | null
}

export type ComposerTakeoverView =
  | { kind: 'none' }
  | { kind: 'approval'; approval: ApprovalView }
  | { kind: 'question'; question: QuestionView }

/**
 * Capability detection result for one connection.
 *
 * `unknown` is a first-class value: a connection that was never tested must not
 * be presented as if it had passed (ARCHITECTURE §15.9).
 */
export type CapabilityResult = 'passed' | 'failed' | 'unknown'

export interface CapabilityView {
  name: string
  result: CapabilityResult
}

export type ConnectionStatus = 'unverified' | 'testing' | 'verified' | 'failed' | 'unsupported'

/**
 * Transport security of an endpoint (plan §6.8).
 *
 * Non-loopback endpoints require HTTPS by default. A LAN HTTP endpoint can only
 * be saved after the user acknowledges the plaintext risk, and it keeps a visible
 * mark afterwards. Loopback HTTP is not flagged.
 */
export type TransportKind = 'https' | 'loopback-http' | 'plaintext-http'

export interface ModelConnectionView {
  id: string
  name: string
  /** Non-secret endpoint, shown so the user can tell providers apart. */
  baseUrl: string
  transport: TransportKind
  modelId: string
  contextWindowLabel: string
  isDefault: boolean
  /** The key value is never available to the page, only its presence. */
  keyStatus: 'configured' | 'missing' | 'session-only'
  status: ConnectionStatus
  /** Short factual line about the last check; never raw provider output. */
  statusDetail: string | null
  capabilities: readonly CapabilityView[]
  /**
   * 用户声明：这条连接**支持图片输入**（2026-10-16）。
   *
   * 与 `capabilities`（实测报告）分开：收不收图只能靠发图试，"测不出来"与"不支持"是两回事。
   * 可选以兼容原型夹具——缺省视为"没声明"，页面就不显示这一条。
   */
  supportsVision?: boolean
  /** The card is asking the user to confirm deactivation. */
  confirmDelete: boolean
  /** Editing produced a new version that only applies to the next request. */
  pendingApply: boolean
}

/** 权限档位；与契约同源（2026-10-15 换成"能写到哪"的三档）。 */
export type ExternalPathsMode = 'read-only' | 'workspace-write' | 'danger-full-access'

/** 一条授权给出的访问级别；"不给访问"用撤销表达，不另立一个级别。 */
export type ExternalPathAccess = 'read' | 'read_write'

export interface ExternalPathGrantView {
  /** 用户自己填进来的目录，已由服务端规范化（大小写、分隔符、符号链接）。 */
  path: string
  access: ExternalPathAccess
}

/**
 * 「设置 → 权限」分区（T38）。
 *
 * `defaultMode` 是 `config.yaml` 的出厂默认，用来解释"库里没有用户选择时按哪一档跑"；
 * `storeUnavailable` 为真时必须如实说明"这里改了也存不下来"，而不是让页面看起来保存成功。
 * 授权条目决定工作区外目标的访问级别（`read-only` 档把 `read_write` 降级为 `read`）；
 * 写侧沙箱由档位派生，不再有独立开关，因此这里没有沙箱字段。
 */
export interface PermissionsView {
  mode: ExternalPathsMode
  defaultMode: ExternalPathsMode
  grants: readonly ExternalPathGrantView[]
  storeUnavailable: boolean
}

/**
 * 能力不可用的原因（v0.5 T56；契约 §16.1、§16.6）。
 *
 * 后端给的是**闭集**，页面不许自造第四种：`disabled` = 开关关着（用户自己打开即可）、
 * `missing_dependency` = 这台机器缺东西（例如没装 git）。
 *
 * 2026-10-13：档位退出运行期绑定，原先的 `profile`（"换个档位再试"）随之删除——页面上已经没有
 * 换档这个动作，留着它只会渲染出一句做不到的建议。
 */
export type CapabilityReason = 'available' | 'disabled' | 'missing_dependency'

/**
 * 一条能力状态。
 *
 * 这里**只有能力与解释，没有工具名**：用户要判断的是"现在能不能联网、能不能提交"，
 * 不是内部绑了哪几个工具。`available` 为假时 `hint` 必须有——只灰掉不解释，
 * 用户会以为功能坏了。
 */
export interface CapabilityStatusView {
  capability: string
  label: string
  available: boolean
  reason: CapabilityReason
  detail: string
  hint: string | null
  /** 不可关闭的基础能力：页面把它画成"始终开启"，而不是给一个点不动的控件。 */
  locked: boolean
}

/** 能力开关表与每一项的可用性；每次由服务端现算（取决于开关与本机环境）。 */
export interface CapabilitiesView {
  /** 能力名 → 是否开启（闭集里每一项都有，含不可关闭的那两个）。 */
  switches: Readonly<Record<string, boolean>>
  /** **可开关**的能力（服务端枚举，固定顺序）。页面照它渲染，不自己拼一份（§16.1）。 */
  switchable: readonly string[]
  /** 没有可用设置库：开关只能来自 `config.yaml`，此时**不提供**开关。 */
  storeUnavailable: boolean
  capabilities: readonly CapabilityStatusView[]
}

/**
 * 一个技能在管理页上的样子（技能管理页，2026-10-16；契约 §16.5）。
 *
 * **没有技能目录的绝对路径**：技能常在用户主目录下，管理页不需要知道主目录结构。
 * 三个布尔各答一件事：`enabled` 是**用户**的选择，`modelInvocable` / `userInvocable` 是
 * **技能作者**在 frontmatter 里的声明。
 */
export interface SkillEntryView {
  name: string
  description: string
  /** 发现它的**来源层**展示名（例如"个人级"）——不是路径。 */
  source: string
  /** 用户是否允许使用它（库里只记被禁用的那些，新技能默认可用）。 */
  enabled: boolean
  /** 作者声明：是否允许模型自己调用（`disable-model-invocation: true` 时为假）。 */
  modelInvocable: boolean
  /** 作者声明：是否允许用户手动调用。 */
  userInvocable: boolean
  /** 技能声明的能力（由 `allowed-tools` 映射而来，**只能收窄**本档能力）。 */
  declaredCapabilities: readonly string[]
}

/**
 * 技能清单与启用状态（技能管理页，2026-10-16；契约 §16.5）。
 *
 * `enabled` 是**总闸**（`skill` 能力开关）：它关着时技能对模型完全不可见；逐条开关与它是两件事——
 * 总闸是"这个功能用不用"，逐条是"哪个技能可用"。`diagnostics` 是被发现过程跳过的条目说明，
 * 必须原样展示：不说的话用户只会看到"我的技能怎么不见了"。
 */
/**
 * 一个技能发现根（2026-10-20）。
 *
 * 页面用四件事回答"为什么某个技能没出现"：路径、来源层、这个根下发现了几个、能不能移除。
 * `removable` 只对**用户自己添加**的根为真——随包根与个人级根不允许在这里删。
 */
export interface SkillRootView {
  path: string
  /** 来源层展示名（例如"随包技能"、"个人级（.agents）"、"自定义根"）。 */
  source: string
  /** 这个根下发现了几个技能（0 也显示：用户据此判断路径填错没有）。 */
  count: number
  removable: boolean
  /** 目录当前是否可读；不可读时提示"路径不存在或没有权限"。 */
  readable: boolean
}

export interface SkillsView {
  enabled: boolean
  storeUnavailable: boolean
  skills: readonly SkillEntryView[]
  /** 当前在扫的发现根（随包 / 个人级 / 自定义）。 */
  roots: readonly SkillRootView[]
  diagnostics: readonly string[]
}

/**
 * 任务状态（v0.6 T62；契约 §17.1、§17.6）。
 *
 * 后端给的是**闭集**，页面不许自造第八种。`needs_review` = "服务重启后证明不了任务结局"
 * （§17.5）：可能已部分执行、可能仍有残留进程，**不自动重跑**，由人工核对。
 */
export type TaskState =
  | 'queued'
  | 'running'
  | 'waiting_user'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'needs_review'

/**
 * 任务中心的一行（§17.6）。
 *
 * **没有 `run_id` / `thread_id` / `request_id`，也没有工具名**（§15.8 的字段门槛）：
 * `taskId` 只作列表键与动作参数，页面上一个字都不显示。"在等什么"也不在这里——它是 `state`
 * 的函数，由页面推导（那样就不可能与真源不一致）。
 */
export interface TaskRowView {
  taskId: string
  sessionId: string
  /** 会话标题：任务中心靠它让用户认出"这是哪个会话在跑"。 */
  sessionTitle: string
  state: TaskState
  /** 服务端写好的可读原因（失败 / 取消 / 需核对）；没有就是 `null`。 */
  detail: string | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
}

/** 任务中心的一页；`counts` 覆盖全部任务（不受列表截断影响），没有的状态是 0。 */
export interface TasksView {
  tasks: readonly TaskRowView[]
  counts: Readonly<Record<TaskState, number>>
  /** 等你处理（在等审批 + 需要人工核对）：入口角标用，为 0 时页面不显示角标。 */
  pendingCount: number
}

export interface SettingsView {
  /**
   * v0.4 settings has two usable sections — chat connections and permissions — so
   * the dialog's section switcher is now the real thing, not a nav of inert
   * items. (Rewriting this note and the `SettingsDialog.test.tsx` assertion of
   * the same name is part of T38: the old argument was "do not offer a section we
   * cannot honour", and both sections can be honoured now.)
   */
  connections: readonly ModelConnectionView[]
  /** `'os'` means keys survive a restart; `'unavailable'` means they do not. */
  credentialStore: { kind: 'os' | 'unavailable'; note: string | null }
  /** 权限分区；`null` 表示还没读到——不编造一个档位出来（§15.9）。 */
  permissions: PermissionsView | null
}

export interface ModelMenuItemView {
  id: string
  name: string
  modelId: string
  status: ConnectionStatus
}

export interface ModelMenuView {
  connections: readonly ModelMenuItemView[]
  /** The connection the session currently pins for the next request. */
  selectedId: string | null
  /** What the in-flight request actually uses, when it differs from the selection. */
  pinnedLabel: string | null
  /** Reminder that a change only takes effect on the next submit. */
  nextTurnNotice: string | null
  /** No per-session override: the effective selection comes from the default connection. */
  followsDefault?: boolean
}

/**
 * 一次上下文压缩在对话流里的一行（2026-10-16）。
 *
 * 位置：它是**流程的一部分**，与 Think、工具行同级——所以渲染在消息流里，而不是挂在输入框那圈
 * chrome 上（用户实拍："他不应该出现在对话框的上方"）。数据来自两处，边界很清楚：
 * - `running`（正在压缩）：只有**进程内**状态有，放流尾；
 * - 已完成的那次：来自那一轮的 `events` 里的 compaction 记录（落 Checkpoint、刷新可读）。
 */
export interface CompressionRowView {
  running: boolean
  /** 折叠掉的消息条数（完成时才有；运行中是 `null`）。 */
  foldedMessages: number | null
  /** 压缩前/后的**估算**输入 token（完成时才有）。 */
  tokensBefore: number | null
  tokensAfter: number | null
}

export type MessageView =
  | { kind: 'compression'; id: string; compression: CompressionRowView }
  | { kind: 'user'; id: string; text: string; at: string; attachments?: readonly string[] }
  | { kind: 'assistant'; id: string; text: string; at: string; reasoning?: string;
      reasoningTruncated?: boolean; streaming?: boolean; reasoningRunning?: boolean }
  | { kind: 'tool'; id: string; tool: ToolRowView }
  | { kind: 'error'; id: string; notice: ErrorNoticeView }
  | { kind: 'memory'; id: string; memory: MemoryRowView }

/**
 * One safe citation for a retrieved source.
 *
 * A reference is a locator, never content: v0.4 may show *that* a workspace file
 * or a memory entry was used, not what it said — and never vector scores or raw
 * long-term memory bodies (plan §6.7).
 */
export interface RetrievalSourceView {
  kind: 'workspace-file' | 'long-term-memory' | 'session-history'
  /** Path plus line range, or a memory entry id plus its write time. */
  reference: string
  /** How the source was used in the answer. */
  usage: string
}

/**
 * What retrieval did for this turn.
 *
 * `failed` is deliberately not folded into `miss`: "we looked and found nothing"
 * and "we could not look" are different facts, and only the second one tells the
 * user the answer has no retrieval behind it for a reason.
 */
export type RetrievalView =
  | { outcome: 'hit'; sources: readonly RetrievalSourceView[]; note: string }
  | { outcome: 'miss'; note: string }
  | { outcome: 'failed'; reason: string; note: string }

/**
 * 写入结果的三种事实（§10.4 的闭集）。
 *
 * `skipped` = **有候选但一条都没落库**（例如两层记忆都关着，或候选没落进任何库）。它必须出现并带上
 * 原因：缺了它，用户看到的是"什么都没有"，分不清"这轮没东西可记"与"想记但被跳过了"。
 */
export interface MemoryWriteView {
  outcome: 'written' | 'failed' | 'skipped'
  detail: string
}

export interface MemoryRowView {
  retrieval: RetrievalView
  /** Long-term memory write for this turn; null when the turn wrote nothing. */
  write: MemoryWriteView | null
}

/** Empty conversation states (plan §6.7 / matrix 01–02). */
export interface EmptyStateView {
  title: string
  description: string
  /** 欢迎卡片的标题；与 `highlights` 同时给出时才渲染卡片。 */
  highlightsTitle?: string
  /** 欢迎卡片的要点（这项产品能做什么），每条一行。 */
  highlights?: readonly string[]
  /** 空态底部的补充提示（例如「先去左侧新建会话」），不参与标题层级。 */
  hint?: string
}

/** Loading state for a conversation whose events are still being read. */
export interface LoadingStateView {
  label: string
  detail: string
  /** Skeleton widths as percentages: shape only, no invented content. */
  rows: readonly number[]
}

export interface ComposerStateView {
  /** When set, the input is disabled and this explains why. */
  disabledReason: string | null
  /** Phase shown above the input while a turn is in flight. */
  phase: string | null
  /** Renders the context-meter detail as an open panel instead of a hover popover. */
  meterOpen?: boolean
}

/** Safe activity summary from the current snapshot, never model reasoning. */
export interface RunActivityView {
  id: string
  label: string
  detail: string
  active: boolean
  /** Persisted submission time; null means the elapsed duration is unknown. */
  startedAt: string | null
}

export interface ConversationScenario {
  id: string
  /** What this prototype exists to check. Shown in the prototype banner. */
  purpose: string
  /**
   * Sidebar row this scenario belongs to; two states may share one session.
   * Null means no workspace is selected — the shell then highlights nothing.
   */
  sessionId: string | null
  session: SessionSummaryView
  messages: MessageView[]
  plan: PlanView | null
  stats: TurnStatsView | null
  /** Live snapshots place each persisted turn's statistics after its final message. */
  turnStats?: readonly { afterMessageId: string; stats: TurnStatsView }[]
  context: ContextMeterView
  takeover: ComposerTakeoverView
  /** Non-null when the settings dialog is open over the shell. */
  settings: SettingsView | null
  /**
   * 非空时覆盖层显示「能力与档位」面板（v0.5 T56）。
   *
   * 面板在产品里是**侧栏的 live-only 入口**；原型宿主默认不渲染它，所以既有场景的 DOM 与
   * 截图逐字节不变。要给它一条视觉基线，就只在这个场景上挂一份视图模型——数据是 fixture，
   * 展示组件本来也不认识 fetch。
   */
  capabilities?: CapabilitiesView | null
  /**
   * 非空时覆盖层显示「任务中心」面板（v0.6 T62；§17.6）。
   *
   * 与 `capabilities` 同一套理由：面板在产品里是**侧栏的 live-only 入口**，原型宿主默认不渲染它，
   * 所以既有场景的 DOM 与截图逐字节不变；要给它视觉基线，就只在这个场景上挂一份视图模型
   * （数据是 fixture，展示组件本来也不认识 fetch）。
   */
  tasks?: TasksView | null
  /** Non-null when the composer's model menu is open. */
  modelMenu: ModelMenuView | null
  /** Non-null when the conversation shows an empty state instead of a flow. */
  empty?: EmptyStateView | null
  /** Non-null while the conversation's events are still being read. */
  loading?: LoadingStateView | null
  /** Composer overrides. Omitted means: enabled, no running phase. */
  composer?: ComposerStateView
  activity?: RunActivityView | null
  /** Renders the sidebar as a narrow rail (matrix 24). */
  sidebarCollapsed?: boolean
  /** Which top-bar tab this scenario opens on. Omitted means `conversation`. */
  tab?: 'conversation' | 'trace'
  /**
   * 待发队列（2026-10-28）：本轮还在跑时用户排在后面的那几条。
   *
   * 与 `capabilities` / `tasks` 同一条理由：产品里这是页面自己的界面状态（`stores/outbox.ts`），
   * 默认场景不带它，既有截图逐字节不变；要视觉基线就在这个场景上挂一份 fixture。
   */
  queued?: readonly QueuedMessageFixture[]
}

/** 队列一条的展示形状（与 `stores/outbox.ts` 的 `QueuedMessage` 结构兼容）。 */
export interface QueuedMessageFixture {
  id: string
  text: string
}
