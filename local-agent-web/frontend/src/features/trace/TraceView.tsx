// 轨迹视图（matrix 20 表格态 + 21 检查器）：一个入口，两段信息。
//
// 左侧**一轮一张卡**：卡头是这一轮的问与用量（带标签的网格）、时间轴、以及**只看得到审批与压缩的
// 事件流水**；卡身是这一轮的工具表。点任一行在右侧展开检查器。
//
// 2026-10-16 排版返工（用户实拍："阅读起来都费劲"）。此前一轮是"表格里塞一整行"：用量挤成一串、
// 事件流水把 10 条工具事件铺在表前面（**和下面的工具表完全重复**），真正的工具行被顶到屏幕外；
// 表头停在最上面，离数据行很远。现在三件事各自有位：
//   1. **用量按标签网格**（标签在上、数字在下），终态只在**不是 completed** 时显示（正常结束写一遍是噪音）；
//   2. **事件流水只列"表格里没有的"**——审批与压缩；工具本来就在卡身的表里；
//   3. 时间轴配**图例**，一次调用被首字切成"等首字/解码"两段（dsh Overview 的画法）。
//
// 三条如实搬运的口径不变：
//   1. **未知不写 0**：拿不到的 token / 耗时写"未知"；
//   2. **未上报 ≠ 0%**：`cache_metrics_reported` 为假时缓存写"未上报"；
//   3. **没有记录的那一轮**写"没有用量记录"，而不是显示一行 0。

import { useState } from 'react'

import type {
  ModelCallView,
  TraceEventView,
  TraceRowView,
  TraceTurnView,
  TraceView as TraceViewModel,
  TurnStatsView,
} from '../../fixtures/types'
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
  uncertain: styles.statusFailed ?? '',
}

/** token 数：过千写 `12.3k`、过百万写 `1.0M`；未知返回"未知"（**不写 0**）。 */
function tokenLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return UNKNOWN
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`
  return String(value)
}

/** 缓存命中：**未上报 ≠ 0%**（`cache_metrics_reported` 就是为这件事存在的）。 */
function cacheLabel(stats: TurnStatsView): string {
  if (stats.cacheReported === false) return '未上报'
  const ratio = stats.cacheHitRatio
  if (ratio === null || ratio === undefined) return UNKNOWN
  return `${Math.round(ratio * 100)}%`
}

/** 上下文占用：只有"已用 + 窗口"都给得出时才给百分比。 */
function contextLabel(stats: TurnStatsView): string {
  const used = stats.contextUsedTokens ?? null
  const window = stats.contextWindowTokens ?? null
  if (used === null) return UNKNOWN
  if (window === null || window <= 0) return tokenLabel(used)
  return `${tokenLabel(used)} / ${tokenLabel(window)}`
}

function contextRatioLabel(stats: TurnStatsView): string {
  const used = stats.contextUsedTokens ?? null
  const window = stats.contextWindowTokens ?? null
  if (used === null || window === null || window <= 0) return ''
  return `${((used / window) * 100).toFixed(1)}%`
}

/** 推理 token 合计：一次都没上报就写"未知"（合计成 0 会是谎）。 */
function reasoningLabel(calls: readonly ModelCallView[]): string {
  const values = calls
    .map((call) => call.reasoningTokens ?? null)
    .filter((value): value is number => value !== null)
  if (values.length === 0) return UNKNOWN
  return tokenLabel(values.reduce((sum, value) => sum + value, 0))
}

/** 一格用量：**标签在上、数字在下**（挤在一行读不出"哪个数是哪个"）。 */
function Metric({
  label,
  value,
  note,
  wide = false,
}: {
  label: string
  value: string
  note?: string
  /** 占两格：`50.7k / 655.4k` 这类值在一格里会被折行（读起来像两个数）。 */
  wide?: boolean
}) {
  return (
    <div className={wide ? `${styles.metric} ${styles.metricWide}` : styles.metric}>
      <span className={styles.metricLabel}>{label}</span>
      <span className={styles.metricValue} data-unknown={value === UNKNOWN ? 'true' : 'false'}>
        {value}
        {note === undefined ? null : <span className={styles.metricNote}>{note}</span>}
      </span>
    </div>
  )
}

/** 这一轮的用量。`null` = 契约没有这一轮的记录（旧 Checkpoint / 原型数据）。 */
function TurnMetrics({ stats }: { stats: TurnStatsView | null }) {
  if (stats === null) {
    return <p className={styles.metricsMissing}>没有用量记录（旧记录或原型数据）</p>
  }
  const input = tokenLabel(stats.inputTokens)
  const ratio = contextRatioLabel(stats)
  return (
    <div className={styles.metrics}>
      {/* 估算值带 `~`：旧 Checkpoint 只有估算输入，绝不能让它冒充供应商实际量。 */}
      <Metric label="输入" value={stats.estimated ? `~${input}` : input} />
      <Metric label="输出" value={tokenLabel(stats.outputTokens)} />
      <Metric label="思考" value={reasoningLabel(stats.calls)} />
      <Metric label="缓存" value={cacheLabel(stats)} />
      <Metric label="首字" value={stats.firstTokenLabel ? stats.firstTokenLabel : UNKNOWN} />
      <Metric label="耗时" value={stats.elapsedLabel ? stats.elapsedLabel : UNKNOWN} />
      <Metric label="上下文" value={contextLabel(stats)} note={ratio === '' ? undefined : ratio} wide />
      {/* 终态只在**不是 normal 结束**时才写：每轮都写一遍 `completed` 是纯噪音。 */}
      {stats.finalState === null || stats.finalState === undefined || stats.finalState === 'completed'
        ? null
        : <Metric label="终态" value={stats.finalState} />}
    </div>
  )
}

/**
 * 本轮逐次模型调用：一次一行，缓存读/写、首字、思考 token 在这里才看得见。
 *
 * **超过 4 次就默认收起**：一次长轮次能有十几行，铺开会把卡身那张工具表顶到屏幕外——而
 * "这一轮做了哪些操作"才是这一页的主内容。收起不是删除：点开就是完整账目。
 */
function CallList({ calls }: { calls: readonly ModelCallView[] }) {
  if (calls.length === 0) return null
  if (calls.length > 4) {
    return (
      <details className={styles.callsBox}>
        <summary className={styles.callsSummary}>
          本轮 {calls.length} 次模型调用（展开看逐次账目）
        </summary>
        <CallRows calls={calls} />
      </details>
    )
  }
  return <CallRows calls={calls} />
}

function CallRows({ calls }: { calls: readonly ModelCallView[] }) {
  return (
    <ol className={styles.calls} aria-label="本轮的模型调用">
      {calls.map((call) => {
        const cacheRead =
          call.cacheReadTokens === null || call.cacheReadTokens === undefined
            ? '缓存未上报'
            : `缓存读 ${tokenLabel(call.cacheReadTokens)}`
        const wait = call.firstTokenLabel ? `首字 ${call.firstTokenLabel}` : `首字 ${UNKNOWN}`
        return (
          <li key={call.id} className={styles.call}>
            <span className={styles.callModel} title={call.modelLabel}>
              {call.modelLabel}
            </span>
            <span className={styles.callPurpose}>{call.purpose}</span>
            <span className={styles.callNumbers}>{wait}</span>
            <span className={styles.callNumbers}>
              {call.elapsedLabel ? call.elapsedLabel : UNKNOWN}
            </span>
            <span className={styles.callNumbers}>输入 {tokenLabel(call.inputTokens)}</span>
            <span className={styles.callNumbers}>{cacheRead}</span>
            <span className={styles.callNumbers}>输出 {tokenLabel(call.outputTokens)}</span>
            <span className={styles.callNumbers}>思考 {tokenLabel(call.reasoningTokens ?? null)}</span>
          </li>
        )
      })}
    </ol>
  )
}

/** 事件与时间轴的种类标签。 */
const KIND_LABEL: Record<TraceEventView['kind'], string> = {
  tool: '工具',
  approval: '审批',
  compaction: '压缩',
}

const SPAN_CLASS: Record<Span['kind'], string> = {
  'model-wait': styles.spanWait ?? '',
  'model-decode': styles.spanDecode ?? '',
  tool: styles.spanTool ?? '',
  approval: styles.spanApproval ?? '',
  compaction: styles.spanCompaction ?? '',
}

/** 毫秒 → 人话（时长口径，**不用钟点**：钟点受时区影响，断言和阅读都会漂）。 */
function msLabel(value: number | null): string {
  if (value === null) return UNKNOWN
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${value}ms`
}

interface Span {
  id: string
  kind: TraceEventView['kind'] | 'model-wait' | 'model-decode'
  from: number
  to: number
  title: string
}

/** 事件/调用 → 时间轴上的一段；时刻缺失或不可解析就**不画**（不拿别的时刻凑）。 */
function toSpan(
  id: string,
  kind: Span['kind'],
  startIso: string,
  endIso: string,
  title: string,
): Span | null {
  const from = Date.parse(startIso)
  const to = Date.parse(endIso)
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return null
  return { id, kind, from, to, title }
}

function eventTitle(event: TraceEventView): string {
  const head = `${KIND_LABEL[event.kind]} · ${event.label}`
  if (event.kind === 'approval') return `${head}（等待 ${msLabel(event.waitMs)}）`
  if (event.kind === 'compaction') {
    const before = event.tokensBefore
    const after = event.tokensAfter
    if (before === null || after === null) return head
    return `${head}（${tokenLabel(before)} → ${tokenLabel(after)}）`
  }
  return event.status === null ? head : `${head}（${event.status}）`
}

/**
 * 本轮的全部 span：**事件流水**（工具/审批/压缩）+ **逐次模型调用**。
 *
 * 每次调用被首字切成两截——"等首字"（TTFT）与"解码"：这是"这一轮为什么慢"最直接的回答。
 */
function turnSpans(turn: TraceTurnView): Span[] {
  const spans: Span[] = []
  for (const event of turn.events) {
    const span = toSpan(
      event.id,
      event.kind,
      event.at,
      event.finishedAt === '' ? event.at : event.finishedAt,
      eventTitle(event),
    )
    if (span !== null) spans.push(span)
  }
  for (const call of turn.stats?.calls ?? []) {
    const start = call.startedAt ?? ''
    const first = call.firstTokenAt ?? ''
    const end = call.finishedAt ?? ''
    if (start !== '' && first !== '') {
      const wait = toSpan(`${call.id}:wait`, 'model-wait', start, first, `${call.modelLabel} 等首字`)
      if (wait !== null) spans.push(wait)
    }
    const decodeFrom = first !== '' ? first : start
    if (decodeFrom !== '' && end !== '') {
      const decode = toSpan(
        `${call.id}:decode`,
        'model-decode',
        decodeFrom,
        end,
        `${call.modelLabel} 解码`,
      )
      if (decode !== null) spans.push(decode)
    }
  }
  return spans
}

const SPAN_LABEL: Record<Span['kind'], string> = {
  'model-wait': '等首字',
  'model-decode': '解码',
  tool: '工具',
  approval: '审批',
  compaction: '压缩',
}

/**
 * **时间构成**：把 span 按种类相加，从大到小排列。
 *
 * 为什么不画那根"全 span 的条"：一轮里有 100+ 条工具事件时，2px 高的条会变成条形码
 * （2026-10-16 用户实拍："阅读起来都费劲"）。数字能一眼读出"时间花在哪"，条不能。
 *
 * **刻意不给合计**：并行的工具调用在时间上会重叠，把各段相加当成总时长是在编数。
 */
function timeBreakdown(spans: readonly Span[]): readonly { kind: Span['kind']; ms: number }[] {
  const totals = new Map<Span['kind'], number>()
  for (const span of spans) {
    totals.set(span.kind, (totals.get(span.kind) ?? 0) + (span.to - span.from))
  }
  return [...totals.entries()]
    .filter(([, ms]) => ms > 0)
    .sort((left, right) => right[1] - left[1])
    .map(([kind, ms]) => ({ kind, ms }))
}

function TimeBreakdown({ spans }: { spans: readonly Span[] }) {
  const items = timeBreakdown(spans)
  if (items.length === 0) return null
  return (
    <span
      className={styles.breakdown}
      title="按种类相加；并行的调用在时间上会重叠，所以这里不求和"
    >
      <span className={styles.breakdownLabel}>时间构成</span>
      {items.map((item) => (
        <span key={item.kind} className={styles.breakdownItem}>
          <span className={`${styles.legendDot} ${SPAN_CLASS[item.kind]}`} aria-hidden="true" />
          {SPAN_LABEL[item.kind]} {msLabel(item.ms)}
        </span>
      ))}
    </span>
  )
}

/**
 * 卡头下的第三层：**时间轴**（回答"时间花在哪"）+ **事件流水**（回答"发生过什么"）。
 *
 * 流水**只列审批与压缩**：工具在下面的表里已经有名字、目标、结果与耗时，再列一遍等于把表顶下去
 * （重构前就是这样，10 条 `工具 … ok` 铺在表前面）。时间轴仍然画全部 span（工具也要在轴上）。
 */
function TurnEventLine({ turn }: { turn: TraceTurnView }) {
  const spans = turnSpans(turn)
  const notable = turn.events.filter((event) => event.kind !== 'tool')
  if (spans.length === 0 && notable.length === 0) return null
  const from = spans.length === 0 ? 0 : Math.min(...spans.map((span) => span.from))
  const to = spans.length === 0 ? 0 : Math.max(...spans.map((span) => span.to))
  const width = Math.max(1, to - from)
  // 关键节点条上**只画审批与压缩**：它们是"需要人看"的两个时刻，而且数量少；工具与模型调用
  // 已经由下面的表与逐次账目交代，全画上去就是条形码。
  const keySpans = spans.filter((span) => span.kind === 'approval' || span.kind === 'compaction')
  return (
    <div className={styles.eventBlock}>
      {spans.length === 0 ? null : <TimeBreakdown spans={spans} />}
      {keySpans.length === 0 ? null : (
        <span
          className={styles.timeline}
          role="img"
          aria-label={`第 ${turn.index} 轮的关键节点：审批与压缩`}
        >
          {keySpans.map((span) => (
            <span
              key={span.id}
              className={`${styles.span} ${SPAN_CLASS[span.kind]}`}
              style={{
                left: `${((span.from - from) / width) * 100}%`,
                // 极短的段给一个下限宽度，否则它在那条轴上等于不存在。
                width: `${Math.max(0.5, ((span.to - span.from) / width) * 100)}%`,
              }}
              title={span.title}
            />
          ))}
        </span>
      )}
      {notable.length === 0 ? null : (
        <ul className={styles.eventList}>
          {notable.map((event) => (
            <li key={event.id} className={styles.event}>
              <span className={styles.eventKind} data-kind={event.kind}>
                {KIND_LABEL[event.kind]}
              </span>
              <span className={styles.eventLabel}>{event.label}</span>
              <span className={styles.eventMeta}>
                {event.kind === 'approval' && event.waitMs !== null
                  ? `等待 ${msLabel(event.waitMs)}`
                  : null}
                {event.kind === 'compaction' && event.foldedMessages !== null
                  ? `折叠 ${event.foldedMessages} 条`
                  : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function TraceRowCells({
  row,
  isSelected,
  onSelect,
}: {
  row: TraceRowView
  isSelected: boolean
  onSelect: () => void
}) {
  const summary = resultSummary(row.resultPreview)
  return (
    <>
      <td className={styles.typeCell}>
        {/* 行级点击负责鼠标，这个按钮负责键盘：两者指向同一次选择。 */}
        <button
          type="button"
          className={styles.typeButton}
          aria-pressed={isSelected}
          aria-label={`查看${toolLabel(row.toolName)}（${TOOL_STATUS_LABEL[row.status]}）的详情`}
          onClick={(event) => {
            event.stopPropagation()
            onSelect()
          }}
        >
          <span className={`${styles.status} ${STATUS_CLASS[row.status]}`} aria-hidden="true" />
          {toolLabel(row.toolName)}
        </button>
      </td>
      <td className={styles.payloadCell}>{row.target ?? UNKNOWN}</td>
      <td className={styles.resultCell}>
        {summary === '' ? UNKNOWN : summary}
        {row.resultTruncated ? <span className={styles.truncated}>（已截断）</span> : null}
      </td>
      <td className={styles.timeCell}>{row.durationLabel === '' ? UNKNOWN : row.durationLabel}</td>
    </>
  )
}

interface TraceViewProps {
  trace: TraceViewModel
}

export function TraceView({ trace }: TraceViewProps) {
  // 选中态是**界面状态**，不属于服务端契约：刷新后不保留，也不假装是服务端的选择。
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = trace.rows.find((row) => row.id === selectedId) ?? null
  const grouped = trace.turns.length > 0
  // 没有轮次分组（原型 fixture / 旧快照）时回落到一张平铺表——**不丢行**。
  const groups: readonly TraceTurnView[] = grouped
    ? trace.turns
    : [{ id: '__all__', index: -1, prompt: '', stats: null, rows: trace.rows, events: [] }]
  const nothing = trace.rows.length === 0 && trace.turns.length === 0

  return (
    <div className={styles.split}>
      <div className={styles.tablePane} role="region" aria-label="轨迹">
        {nothing ? (
          <p className={styles.empty}>这个会话还没有操作记录。</p>
        ) : (
          <div className={styles.turns}>
            {groups.map((group) => (
              <section key={group.id} className={styles.turn} aria-label={grouped ? (group.index === 0 ? '会话开始' : `第 ${group.index} 轮`) : '全部操作'}>
                {grouped ? (
                  <header className={styles.turnCard}>
                    <div className={styles.turnHead}>
                      <span className={styles.turnIndex}>
                        {group.index === 0 ? '会话开始' : `第 ${group.index} 轮`}
                      </span>
                      {group.prompt === '' ? null : (
                        <span className={styles.turnPrompt} title={group.prompt}>
                          {group.prompt}
                        </span>
                      )}
                    </div>
                    <TurnMetrics stats={group.stats} />
                    <TurnEventLine turn={group} />
                    {group.stats === null ? null : <CallList calls={group.stats.calls} />}
                  </header>
                ) : null}
                {group.rows.length === 0 ? (
                  <p className={styles.turnEmpty}>
                    {grouped ? '这一轮没有工具调用。' : '这个会话没有工具调用记录。'}
                  </p>
                ) : (
                  <table className={styles.table}>
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
                      {group.rows.map((row) => {
                        const isSelected = row.id === selectedId
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
                            <TraceRowCells
                              row={row}
                              isSelected={isSelected}
                              onSelect={() => {
                                setSelectedId(row.id)
                              }}
                            />
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </section>
            ))}
          </div>
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
