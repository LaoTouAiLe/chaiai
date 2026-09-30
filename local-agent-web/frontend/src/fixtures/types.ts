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
    kind: 'todo'
    items: readonly { text: string; status: 'completed' | 'in_progress' | 'pending' }[]
  }
  | { kind: 'generic'; headline: string; detail: string }

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

export interface ToolRowView {
  /** Tool name as the user sees it. */
  name: string
  /** Short human summary of what the call is doing. */
  summary: string
  status: 'running' | 'succeeded' | 'failed'
  /** Pre-formatted duration; measuring timings is T34's job. */
  durationLabel: string
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
  /** `true` when the token numbers are local estimates rather than provider facts. */
  estimated: boolean
  /** One entry per model call in this turn, for the expanded breakdown. */
  calls: readonly ModelCallView[]
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
  status: 'running' | 'succeeded' | 'failed'
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
export interface TraceView {
  rows: readonly TraceRowView[]
  calls: readonly ModelCallView[]
}

export interface SessionSummaryView {
  title: string
  /** Null while no workspace is selected: the shell then shows no breadcrumb. */
  workspace: string | null
  status: 'idle' | 'running' | 'awaiting-approval' | 'completed'
  modelLabel: string
}

export interface ContextMeterView {
  usedTokens: number | null
  windowTokens: number | null
  /** Whether the numbers describe the in-flight request or the previous one. */
  scope: 'current' | 'previous' | 'unknown'
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
  state: 'pending' | 'executing' | 'approved' | 'rejected' | 'expired'
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

export type ComposerTakeoverView = { kind: 'none' } | { kind: 'approval'; approval: ApprovalView }

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
  /** The card is asking the user to confirm deactivation. */
  confirmDelete: boolean
  /** Editing produced a new version that only applies to the next request. */
  pendingApply: boolean
}

export interface SettingsView {
  /**
   * v0.4 settings only manage chat connections, so the dialog has a single
   * section. There is deliberately no section switcher: a nav with an inert
   * item would promise an interaction the prototype does not have.
   */
  connections: readonly ModelConnectionView[]
  /** `'os'` means keys survive a restart; `'unavailable'` means they do not. */
  credentialStore: { kind: 'os' | 'unavailable'; note: string | null }
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

export type MessageView =
  | { kind: 'user'; id: string; text: string; at: string }
  | { kind: 'assistant'; id: string; text: string; at: string }
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

export interface MemoryWriteView {
  outcome: 'written' | 'failed'
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
  /** Non-null when the composer's model menu is open. */
  modelMenu: ModelMenuView | null
  /** Non-null when the conversation shows an empty state instead of a flow. */
  empty?: EmptyStateView | null
  /** Non-null while the conversation's events are still being read. */
  loading?: LoadingStateView | null
  /** Composer overrides. Omitted means: enabled, no running phase. */
  composer?: ComposerStateView
  /** Renders the sidebar as a narrow rail (matrix 24). */
  sidebarCollapsed?: boolean
  /** Which top-bar tab this scenario opens on. Omitted means `conversation`. */
  tab?: 'conversation' | 'trace'
}
