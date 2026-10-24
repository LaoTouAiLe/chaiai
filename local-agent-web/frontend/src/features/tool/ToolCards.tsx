import { useState, type ReactNode } from 'react'
import { Check, Copy, Doc } from '../../components/icons'
import type { ToolCardView } from '../../fixtures/types'
import { copyText } from '../conversation/clipboard'
import { useLineWindow, windowOf, type LineWindow } from './lineWindow'
import styles from './ToolCards.module.css'

interface CardShellProps {
  title: string
  meta: ReactNode
  children: ReactNode
  label: string
}

/**
 * Shared chrome for the non-diff tool cards.
 *
 * Every card shows a workspace-relative identifier, a bounded body and an
 * explicit truncation note when the body was capped — never a silent cut
 * (ARCHITECTURE §15.8).
 */
function CardShell({ title, meta, children, label }: CardShellProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copyLabel = copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : `复制 ${title}`
  return (
    <section className={styles.card} aria-label={label}>
      <header className={styles.header}>
        <span className={styles.icon} aria-hidden="true">
          <Doc />
        </span>
        <span className={styles.title}>{title}</span>
        <span className={styles.meta}>{meta}</span>
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
    >
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
    >
      {card.shape === 'matches' ? (
        <ul className={styles.groups}>
          {groups.map((group) => (
            <li key={group.path} className={styles.group}>
              <div className={styles.groupPath}>{group.path}</div>
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
              {path}
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
              <pre className={styles.terminal}>{visibleStderr.join('\n')}</pre>
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

function TodoCard({ card }: { card: Extract<ToolCardView, { kind: 'todo' }> }) {
  const done = card.items.filter((item) => item.status === 'completed').length
  return (
    <CardShell
      label="任务清单"
      title="任务清单"
      meta={`${String(done)}/${String(card.items.length)} 已完成`}
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
function DelegationCard({ card }: { card: Extract<ToolCardView, { kind: 'delegation' }> }) {
  const done = card.children.filter((child) => child.status === 'completed').length
  const lines = card.report === '' ? [] : card.report.split('\n')
  const lineWindow = useLineWindow(lines.length)
  return (
    <CardShell
      label="委派只读调查"
      title="委派只读调查"
      meta={`${String(done)}/${String(card.children.length)} 个子任务已完成`}
    >
      <ul className={styles.delegations}>
        {card.children.map((child, index) => (
          <li key={`${String(index)}:${child.scope}`} className={styles.delegation}>
            <span className={styles.delegationMark} aria-hidden="true">
              {child.status === 'completed' ? <Check /> : null}
            </span>
            <span className={styles.delegationScope}>{child.scope}</span>
            <span className={styles.delegationMeta}>
              {child.status === 'completed'
                // 只给用户能据以判断的三项（证据、工具次数、状态）。**不写"预留 token"**：那是切出来的
                // 上限、每路几乎一样，摆在这里会被读成"这一路花了多少"（2026-10-13 裁定）。
                ? `已完成 · 证据 ${String(child.evidenceCount)} · 工具 ${String(child.toolCalls)} 次`
                : `未完成 · ${child.error ?? '原因未记录'}`}
            </span>
          </li>
        ))}
      </ul>
      {lines.length === 0
        ? null
        : <pre className={styles.terminal}>{windowOf(lines, lineWindow.shown).join('\n')}</pre>}
      <MoreLines lineWindow={lineWindow} />
      {card.truncated ? (
        <p className={styles.note}>内容较长，页面里是服务端给的有界预览；完整原文仍在本地工具事件中。</p>
      ) : null}
    </CardShell>
  )
}

function GenericCard({ card }: { card: Extract<ToolCardView, { kind: 'generic' }> }) {
  const lines = card.detail.split('\n')
  const lineWindow = useLineWindow(lines.length)
  return (
    <CardShell label="工具结果" title={card.headline} meta="结果详情">
      <p className={styles.generic}>{windowOf(lines, lineWindow.shown).join('\n')}</p>
      <MoreLines lineWindow={lineWindow} />
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
export function ToolCard({ card }: { card: ToolCardView }) {
  switch (card.kind) {
    case 'read':
      return <ReadCard card={card} />
    case 'search':
      return <SearchCard card={card} />
    case 'terminal':
      return <TerminalCard card={card} />
    case 'result':
      return <ResultCard card={card} />
    case 'todo':
      return <TodoCard card={card} />
    case 'delegation':
      return <DelegationCard card={card} />
    case 'generic':
      return <GenericCard card={card} />
    /* v8 ignore next 2 -- file mutations render through DiffCard in ToolRow */
    case 'file-mutation':
      return <GenericCard card={{ kind: 'generic', headline: card.diff.path, detail: '见完整 diff 卡' }} />
  }
}
