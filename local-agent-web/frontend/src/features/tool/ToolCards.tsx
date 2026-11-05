import { useContext, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { getDelegationLive, type DelegationLiveLaneDto } from '../../api/client'
import { Check, ChevronDown, ChevronRight, Copy, Doc } from '../../components/icons'
import type { ToolCardView } from '../../fixtures/types'
import { copyText } from '../conversation/clipboard'
import { FileLinkContext } from '../conversation/fileLinkContext'
import { filePathFromHref } from '../conversation/fileLinks'
import { StructuredResult } from './StructuredResult'
import { useLineWindow, windowOf, type LineWindow } from './lineWindow'
import styles from './ToolCards.module.css'

interface CardShellProps {
  title: string
  meta: ReactNode
  children: ReactNode
  label: string
  copyBody?: string
}

/**
 * Shared chrome for the non-diff tool cards.
 *
 * Every card shows a workspace-relative identifier, a bounded body and an
 * explicit truncation note when the body was capped — never a silent cut
 * (ARCHITECTURE §15.8).
 */
function CardShell({ title, meta, children, label, copyBody }: CardShellProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const [bodyCopied, setBodyCopied] = useState<boolean | null>(null)
  const [wrap, setWrap] = useState(false)
  const copyLabel = copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : `复制 ${title}`
  return (
    <section className={styles.card} aria-label={label} data-wrap={wrap}>
      <header className={styles.header}>
        <span className={styles.icon} aria-hidden="true">
          <Doc />
        </span>
        <span className={styles.title} title={title}>{title}</span>
        <span className={styles.meta}>{meta}</span>
        {copyBody ? <div className={styles.resultActions}>
          <button type="button" aria-label="结果自动换行" aria-pressed={wrap} onClick={() => setWrap(value => !value)}>换行</button>
          <button type="button" aria-label="复制结果" title="复制本次工具返回的预览原文" onClick={() => { void copyText(copyBody).then(setBodyCopied) }}>{bodyCopied === null ? '复制结果' : bodyCopied ? '已复制结果' : '复制失败'}</button>
        </div> : null}
        <button type="button" className={styles.copy} aria-label={copyLabel} title={copyLabel} onClick={() => {
          void copyText(title).then((ok) => setCopyState(ok ? 'copied' : 'failed'))
        }}>
          <Copy />
        </button>
      </header>
      {children}
    </section>
  )
}

function ResultFile({ path }: { path: string }) {
  const openFile = useContext(FileLinkContext)
  const safePath = filePathFromHref(path)
  return safePath && openFile ? <button type="button" className={styles.fileTarget} title={path}
    aria-label={`打开文件 ${path}`} onClick={() => openFile(safePath)}>{path}<span aria-hidden="true"> ↗</span></button> : <span>{path}</span>
}

/**
 * 长正文的"显示更多"（v0.8 T73；契约 §19.3）。
 *
 * 只在**确有没显示的行**时渲染：正文短于一屏时这里什么都不输出，卡片 DOM 与视觉基线
 * 与从前逐字相同。文案只说"还有多少行没显示"——行号由契约字段给出（`read` 的 `first_line`、
 * `search` 的 `line_number`），这里再编一套必然与卡片里的号对不上。
 */
function MoreLines({ lineWindow }: { lineWindow: LineWindow }) {
  if (!lineWindow.more) return null
  return (
    <div className={styles.more}>
      <span className={styles.moreCount}>{lineWindow.label}</span>
      <button type="button" className={styles.moreButton} onClick={lineWindow.showMore}>
        显示更多
      </button>
    </div>
  )
}

function ReadCard({ card }: { card: Extract<ToolCardView, { kind: 'read' }> }) {
  const lineWindow = useLineWindow(card.lines.length)
  const lines = windowOf(card.lines, lineWindow.shown)
  // 行号直接读视图模型：适配层已经用契约的 `first_line` 把它摆成**文件**行号（§19.3），
  // 组件不再重编号——"把窗口当成第 1 行起"正是这里从前的老毛病。
  const first = lines[0]?.number ?? 1
  const last = lines[lines.length - 1]?.number ?? first
  return (
    <CardShell
      label="读取结果"
      title={card.path}
      meta={`第 ${String(first)}–${String(last)} 行 / 共 ${String(card.totalLines)} 行`}
      copyBody={card.lines.map(line => line.text).join('\n')}
    >
      {card.note ? <p className={styles.note} aria-label="读取说明">{card.note}</p> : null}
      <div className={styles.code}>
        {lines.map((line) => (
          <div key={line.number} className={styles.codeRow}>
            <span className={styles.codeGutter}>{line.number}</span>
            <span className={styles.codeText}>{line.text}</span>
          </div>
        ))}
      </div>
      <MoreLines lineWindow={lineWindow} />
      {card.truncated === true ? (
        <p className={styles.note}>正文较长，页面里是服务端给的有界预览；完整内容可让助手按行区间读取。</p>
      ) : null}
    </CardShell>
  )
}

function SearchCard({ card }: { card: Extract<ToolCardView, { kind: 'search' }> }) {
  const countsMatches = card.shape === 'matches' && card.totalUnit === 'matches'
  // 窗口按**一条序列**算（matches 拍平、paths 本来就是一条），但渲染结构不动：分组照旧是
  // 分组，只是被窗口整个裁掉的组不再渲染。这样"还有多少行没显示"与屏幕上少掉的行数一致。
  const rowCount = card.shape === 'matches'
    ? card.groups.reduce((sum, group) => sum + group.matches.length, 0)
    : card.paths.length
  const lineWindow = useLineWindow(rowCount)
  let budget = lineWindow.shown
  const groups = card.groups.flatMap((group) => {
    const visible = group.matches.slice(0, Math.max(0, budget))
    budget -= visible.length
    // 本来就没有匹配的组照旧渲染（空组不是"被裁掉"）；有匹配但一行都没轮上的组才去掉。
    return visible.length === 0 && group.matches.length > 0 ? [] : [{ path: group.path, matches: visible }]
  })
  const paths = card.shape === 'matches' ? [] : windowOf(card.paths, lineWindow.shown)
  const shown = countsMatches ? groups.reduce((sum, group) => sum + group.matches.length, 0)
    : card.shape === 'matches' ? groups.length : paths.length
  const unit = countsMatches ? '处匹配' : card.shape === 'matches' ? '个文件' : '个路径'
  return (
    <CardShell
      label="搜索结果"
      title={card.shape === 'matches' ? '按内容匹配' : '按路径匹配'}
      meta={`显示 ${String(shown)} ${unit} / 共 ${String(card.total)}${countsMatches ? ` 处匹配 · ${String(card.groups.length)} 个文件` : ''}`}
      copyBody={card.shape === 'matches' ? card.groups.map(group => [group.path, ...group.matches.map(match => `${match.line}: ${match.text}`)].join('\n')).join('\n\n') : card.paths.join('\n')}
    >
      {card.shape === 'matches' ? (
        <ul className={styles.groups}>
          {groups.map((group) => (
            <li key={group.path} className={styles.group}>
              <div className={styles.groupPath}><ResultFile path={group.path} /></div>
              {group.matches.map((match) => (
                <div key={`${group.path}:${String(match.line)}`} className={styles.matchRow}>
                  <span className={styles.codeGutter}>{match.line}</span>
                  <span className={styles.codeText}>{match.text}</span>
                </div>
              ))}
            </li>
          ))}
        </ul>
      ) : (
        <ul className={styles.paths}>
          {paths.map((path) => (
            <li key={path} className={styles.pathRow}>
              <ResultFile path={path} />
            </li>
          ))}
        </ul>
      )}
      <MoreLines lineWindow={lineWindow} />
      {card.truncated ? (
        <p className={styles.note}>
          结果已截断，页面里是服务端给的有界预览；完整内容可让助手按行区间读取，或缩小范围后继续搜索。
        </p>
      ) : null}
    </CardShell>
  )
}

function TerminalCard({ card }: { card: Extract<ToolCardView, { kind: 'terminal' }> }) {
  const exited = card.exitCode !== null || card.signal !== null
  // Only a non-zero exit or a signal is a failure: exit code 0 is a success and
  // must never be painted with the failure treatment.
  const failed = card.signal !== null || (card.exitCode !== null && card.exitCode !== 0)
  // Real shell calls carry stdout/stderr as separate facts; prototype fixtures do
  // not, and keep rendering the single `output` body unchanged.
  const hasStreams = card.stdout !== undefined || card.stderr !== undefined
  // 一条窗口盖住两个流（stdout 在前、stderr 在后，与显示顺序一致）：窗口没到末尾时，
  // 末尾那一段自然只露出一部分。窗口短于正文时一切照旧——`split`/`join` 是恒等变换。
  const stdoutLines = card.stdout === undefined ? [] : card.stdout.split('\n')
  const stderrLines = card.stderr === undefined ? [] : card.stderr.split('\n')
  const bodyLines = card.output.split('\n')
  const lineWindow = useLineWindow(
    hasStreams ? stdoutLines.length + stderrLines.length : bodyLines.length,
  )
  const visibleStdout = windowOf(stdoutLines, lineWindow.shown)
  const visibleStderr = windowOf(stderrLines, Math.max(0, lineWindow.shown - stdoutLines.length))
  // 窗口还没走到 stderr 就先不渲染它那一块：否则屏幕上会出现"标准错误输出："配一段空白，
  // 读起来像"stderr 是空的"，那是**与事实相反**的（事实是"还没轮到这里"，由窗口文案说明）。
  // 窗口没裁掉任何东西时（原型夹具）一律照旧渲染，DOM 与基线不变。
  const showStderr = card.stderr !== undefined && (visibleStderr.length > 0 || !lineWindow.more)
  return (
    <CardShell
      label="终端输出"
      title={card.command}
      copyBody={hasStreams ? [card.stdout, card.stderr].filter(value => value !== undefined).join('\n') : card.output}
      meta={
        <>
          <span className={styles.cwd}>{card.cwd}</span>
          {exited ? (
            <span className={`${styles.exit} ${failed ? styles.exitFailed : ''}`}>
              {card.exitCode === null ? `信号 ${card.signal ?? ''}` : `退出码 ${String(card.exitCode)}`}
            </span>
          ) : null}
        </>
      }
    >
      {hasStreams ? (
        <>
          {card.stdout === undefined
            ? null
            : <pre className={styles.terminal}>{visibleStdout.join('\n')}</pre>}
          {showStderr ? (
            <>
              <p className={styles.note}>标准错误输出：</p>
              <pre className={`${styles.terminal} ${styles.stderr}`}>{visibleStderr.join('\n')}</pre>
            </>
          ) : null}
        </>
      ) : (
        <pre className={styles.terminal}>{windowOf(bodyLines, lineWindow.shown).join('\n')}</pre>
      )}
      <MoreLines lineWindow={lineWindow} />
      {card.timedOut === true ? (
        <p className={styles.note}>命令超时，已停止执行。</p>
      ) : null}
      {card.collapsedLines > 0 ? (
        <p className={styles.note}>
          输出较长，当前省略了 {String(card.collapsedLines)} 行。
        </p>
      ) : null}
      {card.truncated === true ? (
        <p className={styles.note}>输出较长，页面里是服务端给的有界预览；完整内容可让助手按行区间读取。</p>
      ) : null}
    </CardShell>
  )
}

/**
 * Structured result of a mutating call (`edit_file` / `write_file` / `delete_file`).
 *
 * The body is already-redacted, already-bounded text (a diff or a note). Parsing a
 * diff into hunks is T32's job; here the row shows the summary and the safe body
 * instead of re-deriving structure from prose.
 */
function ResultCard({ card }: { card: Extract<ToolCardView, { kind: 'result' }> }) {
  const lines = card.body === '' ? [] : card.body.split('\n')
  const lineWindow = useLineWindow(lines.length)
  return (
    <CardShell
      label="工具结果"
      title={card.target === '' ? '工具结果' : card.target}
      meta={card.summary}
      copyBody={card.body}
    >
      {card.body === ''
        ? null
        : <pre className={styles.terminal}>{windowOf(lines, lineWindow.shown).join('\n')}</pre>}
      <MoreLines lineWindow={lineWindow} />
      {card.truncated ? (
        <p className={styles.note}>内容较长，页面里是服务端给的有界预览；完整内容可让助手按行区间读取。</p>
      ) : null}
    </CardShell>
  )
}

/**
 * 网页检索 / 读取（2026-10-26）。
 *
 * 与 `ResultCard` 分开的理由是**续读方式不同**：文件按行区间重读，网页正文按 `start_char` 续取，
 * 把后者写进"按行区间读取"的提示里就是错的。正文本身仍走 `pre`，因为服务端给的是纯文本
 * （远端抓取是 Markdown 源码），保留原有换行比套一层排版更重要。
 */
function WebCard({ card }: { card: Extract<ToolCardView, { kind: 'web' }> }) {
  const lines = card.body === '' ? [] : card.body.split('\n')
  const lineWindow = useLineWindow(lines.length)
  return (
    <CardShell
      label="网页结果"
      title={card.target === '' ? '网页结果' : card.target}
      meta={card.summary}
      copyBody={card.body}
    >
      {card.body === ''
        ? null
        : <pre className={styles.terminal}>{windowOf(lines, lineWindow.shown).join('\n')}</pre>}
      <MoreLines lineWindow={lineWindow} />
      {card.truncated ? (
        <p className={styles.note}>正文较长，这里是服务端给的有界预览；需要更多时让助手按 start_char 续读。</p>
      ) : null}
    </CardShell>
  )
}

function TodoCard({ card }: { card: Extract<ToolCardView, { kind: 'todo' }> }) {
  const done = card.items.filter((item) => item.status === 'completed').length
  return (
    <CardShell
      label="任务清单"
      title="任务清单"
      meta={`${String(done)}/${String(card.items.length)} 已完成`}
      copyBody={card.items.map(item => `${item.status === 'completed' ? '[x]' : '[ ]'} ${item.text}`).join('\n')}
    >
      <ul className={styles.todos}>
        {card.items.map((item) => (
          <li key={item.text} className={`${styles.todo} ${styles[`todo_${item.status}`] ?? ''}`}>
            <span className={styles.todoMark} aria-hidden="true">
              {item.status === 'completed' ? <Check /> : null}
            </span>
            <span className={styles.todoText}>{item.text}</span>
            <span className={styles.todoStatus}>
              {item.status === 'completed' ? '已完成' : item.status === 'in_progress' ? '进行中' : '待处理'}
            </span>
          </li>
        ))}
      </ul>
    </CardShell>
  )
}

/**
 * 自主委派（§18.7）：每个子 Agent 一行，下面是它核对过的原文。
 *
 * 摆法照参照实现的任务面板（成员 / 状态 / 工具调用次数 / token），而不是把工具返回的 JSON
 * 原样摊开——用户要判断的是"哪一路查到了、花了多少、哪一路没成"，这些必须是字段，不是一句话。
 */
/** 子任务自己的耗时：一秒以下给毫秒（并发时这一档很常见），否则给一位小数的秒。 */
function formatChildElapsed(milliseconds: number): string {
  return milliseconds < 1000 ? `${String(milliseconds)}ms` : `${(milliseconds / 1000).toFixed(1)}s`
}

/**
 * 一路子任务的**独立卡片**（2026-11-04 用户明确要求）。
 *
 * 之前所有子任务挤在一张卡里分行显示——本质还是"一个框"，用户看不出这是两条并行的调查。
 * 现在每一路自己一张卡：标题是它看的目录，正文是它自己的工具链。
 */
function DelegationLaneCard({
  goal,
  scope,
  status,
  meta,
  steps,
}: {
  goal: string
  scope: string
  status: 'running' | 'completed' | 'failed'
  meta: string
  steps: readonly { name: string; target: string }[]
}) {
  // **一律默认收起**（2026-11-04，照 hermes）：块头 + 一行当前活动已经够回答
  // "它在干什么、跑到哪了"，铺开全部步骤会把几条子任务撑成一屏。要看完整链路点一下。
  const [open, setOpen] = useState(false)
  // 收起时要露的那一步：**最后一条**（hermes 的 ticker 只显示当前活动，不铺历史）。
  const latest = steps.length === 0 ? undefined : steps[steps.length - 1]
  // 块头显示"要做什么"（goal），目录退到后面当定位信息（2026-11-04，抄 Hermes / Codex）。
  // goal 缺失时（进程内记录过期）退回只显示目录，不编造任务描述。
  const showGoal = goal !== ''
  return (
    <section className={styles.lane} aria-label="委派子任务" data-status={status}>
      <button
        type="button"
        className={styles.laneHead}
        aria-expanded={open}
        onClick={() => { setOpen((value) => !value) }}
      >
        <span className={styles.laneChevron} aria-hidden="true">
          {open ? <ChevronDown /> : <ChevronRight />}
        </span>
        {status === 'running' ? (
          <span className={`${styles.delegationMark} ${styles.delegationRunning}`} aria-hidden="true" />
        ) : null}
        {status === 'completed' ? <Check /> : null}
        <span className={showGoal ? styles.laneGoal : styles.laneScope}>
          {showGoal ? goal : scope}
        </span>
        {showGoal ? <span className={styles.laneSub}>{scope}</span> : null}
        <span className={styles.laneMeta}>{meta}</span>
      </button>
      {open === false ? (
        // 收起时只露**当前那一步**（hermes 的 ticker 做法）：一行，随最新调用滚动。
        // 跑完的那一路没有"当前"，就不占这一行——结果已经在块头里。
        status === 'running' && latest !== undefined ? (
          <p className={styles.laneTicker}>
            <span className={styles.delegationStepName}>{latest.name}</span>
            <span className={styles.delegationStepText}>{latest.target}</span>
          </p>
        ) : null
      ) : steps.length === 0 ? (
        <p className={styles.laneEmpty}>
          {status === 'running' ? '还在读…' : '这一路没有记录到工具调用'}
        </p>
      ) : (
        <ol className={styles.delegationLane}>
          {steps.map((step, index) => (
            <li key={`${String(index)}:${step.name}`} className={styles.delegationStep}>
              <span className={styles.delegationStepName}>{step.name}</span>
              <span className={styles.delegationStepText}>{step.target}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function DelegationCard({
  card,
  elapsed,
}: {
  card: Extract<ToolCardView, { kind: 'delegation' }>
  elapsed?: string | null
}) {
  const lines = card.report === '' ? [] : card.report.split('\n')
  const lineWindow = useLineWindow(lines.length)
  // 只在这张卡还在跑的时候轮询（2026-11-04）：跑完就以快照里的 `children` 为准。
  // 轮询失败不升级成错误态——它只是增强，拿不到就退化成"进行中…"。
  const live = useQuery({
    queryKey: ['delegation-live'],
    queryFn: getDelegationLive,
    enabled: card.pending.length > 0,
    refetchInterval: 1500,
    retry: false,
  })
  const liveLanes = new Map<string, DelegationLiveLaneDto>(
    (live.data?.lanes ?? []).map((lane) => [lane.scope, lane]),
  )

  // 每一路一张卡：在跑的排前面（上面在动、下面有结果），跑完的那一路立刻换成结果卡。
  const running = card.pending
    .filter((scope) => !card.children.some((child) => child.scope === scope))
    .map((scope) => {
      const lane = liveLanes.get(scope)
      return {
        key: `pending:${scope}`,
        scope,
        goal: lane?.goal ?? '',
        status: 'running' as const,
        meta: '进行中…',
        steps: (lane?.steps ?? []).map((step) => ({ name: step.name, target: step.target })),
      }
    })
  const finished = card.children.map((child, index) => ({
    key: `${String(index)}:${child.scope}`,
    scope: child.scope,
    goal: child.goal ?? '',
    status: child.status,
    meta: child.status === 'completed'
      // 只给用户能据以判断的几项（证据、工具次数、写盘次数、耗时）。**不写"预留 token"**：那是切出来的
      // 上限、每路几乎一样，摆在这里会被读成"这一路花了多少"（2026-10-13 裁定）。
      // 「改动 N 次」只在真写过时出现（2026-11-02）：子 Agent 改了文件却只显示"已完成"，
      // 用户就无从判断磁盘动过没有——那也是父 Agent 最容易顺势谎称"只做了调查"的地方。
      // 耗时排最后且可缺省（2026-11-04）：几路并发时它是"哪一路慢"的唯一依据。
      ? `已完成 · 证据 ${String(child.evidenceCount)} · 工具 ${String(child.toolCalls)} 次${
        child.changedFiles > 0 ? ` · 改动 ${String(child.changedFiles)} 次` : ''
      }${child.elapsedMs === null ? '' : ` · ${formatChildElapsed(child.elapsedMs)}`}`
      : `未完成 · ${child.error ?? '原因未记录'}`,
    steps: (child.tools ?? []).map((tool) => ({ name: tool.name, target: tool.target ?? tool.summary })),
  }))

  const lanes = [...running, ...finished]
  const done = finished.filter((lane) => lane.status === 'completed').length
  // 有在跑的才报等待时长：全都跑完还挂一个"已等待 45s"只会让人以为卡住了。
  const waiting = running.length > 0 && typeof elapsed === 'string' && elapsed !== ''

  return (
    <div className={styles.delegationStack}>
      <p className={styles.delegationSummary}>
        {`${String(done)}/${String(lanes.length)} 个子任务已完成`}
        {waiting ? ` · 已等待 ${String(elapsed)}` : ''}
      </p>
      {lanes.map((lane) => (
        <DelegationLaneCard
          key={lane.key}
          goal={lane.goal}
          scope={lane.scope}
          status={lane.status}
          meta={lane.meta}
          steps={lane.steps}
        />
      ))}
      {lines.length === 0 ? null : (
        // 汇总报告与子任务行同一形状（可折叠），但它默认收起：它是核对结论，
        // 不是"正在发生的事"，一上来就铺满屏会把子任务挤下去。
        <details className={styles.lane}>
          <summary className={styles.laneHead}>
            <span className={styles.laneChevron} aria-hidden="true">
              <ChevronRight />
            </span>
            <span className={styles.laneScope}>汇总报告</span>
            <span className={styles.laneMeta}>{`${String(lines.length)} 行`}</span>
          </summary>
          <pre className={styles.terminal}>{windowOf(lines, lineWindow.shown).join('\n')}</pre>
          <MoreLines lineWindow={lineWindow} />
          {card.truncated ? (
            <p className={styles.note}>内容较长，页面里是服务端给的有界预览；完整原文仍在本地工具事件中。</p>
          ) : null}
        </details>
      )}
    </div>
  )
}

function GenericCard({ card }: { card: Extract<ToolCardView, { kind: 'generic' }> }) {
  const lines = card.detail.split('\n')
  const lineWindow = useLineWindow(lines.length)
  return (
    <CardShell label="工具结果" title={card.headline} meta="结果详情" copyBody={card.detail}>
      <StructuredResult text={card.detail} fallback={<><p className={styles.generic}>{windowOf(lines, lineWindow.shown).join('\n')}</p><MoreLines lineWindow={lineWindow} /></>} />
    </CardShell>
  )
}

/**
 * Render a tool card by its declared kind.
 *
 * An unknown or missing kind resolves to the generic card, which is the only
 * renderer allowed to show free text: a wrong payload never gets drawn as a
 * specialised card.
 */
export function ToolCard({
  card,
  elapsed,
}: {
  card: ToolCardView
  /** 这一行已经跑了多久（只有运行中有值）。委派用它显示"已等待 N 秒"，其余卡片不看它。 */
  elapsed?: string | null
}) {
  switch (card.kind) {
    case 'read':
      return <ReadCard card={card} />
    case 'search':
      return <SearchCard card={card} />
    case 'terminal':
      return <TerminalCard card={card} />
    case 'result':
      return <ResultCard card={card} />
    case 'web':
      return <WebCard card={card} />
    case 'todo':
      return <TodoCard card={card} />
    case 'delegation':
      return <DelegationCard card={card} elapsed={elapsed} />
    case 'generic':
      return <GenericCard card={card} />
    /* v8 ignore next 2 -- file mutations render through DiffCard in ToolRow */
    case 'file-mutation':
      return <GenericCard card={{ kind: 'generic', headline: card.diff.path, detail: '见完整 diff 卡' }} />
  }
}
