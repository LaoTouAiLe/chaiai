import { useState, type ReactNode } from 'react'
import { Check, Copy, Doc } from '../../components/icons'
import type { ToolCardView } from '../../fixtures/types'
import { copyText } from '../conversation/clipboard'
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

function ReadCard({ card }: { card: Extract<ToolCardView, { kind: 'read' }> }) {
  const first = card.lines[0]?.number ?? 1
  const last = card.lines[card.lines.length - 1]?.number ?? first
  return (
    <CardShell
      label="读取结果"
      title={card.path}
      meta={`第 ${String(first)}–${String(last)} 行 / 共 ${String(card.totalLines)} 行`}
    >
      <div className={styles.code}>
        {card.lines.map((line) => (
          <div key={line.number} className={styles.codeRow}>
            <span className={styles.codeGutter}>{line.number}</span>
            <span className={styles.codeText}>{line.text}</span>
          </div>
        ))}
      </div>
      {card.truncated === true ? (
        <p className={styles.note}>
          正文较长，仅显示前 {String(card.lines.length)} 行。如需更多，可让助手按行继续读取。
        </p>
      ) : null}
    </CardShell>
  )
}

function SearchCard({ card }: { card: Extract<ToolCardView, { kind: 'search' }> }) {
  const countsMatches = card.shape === 'matches' && card.totalUnit === 'matches'
  const shown = countsMatches ? card.groups.reduce((sum, group) => sum + group.matches.length, 0)
    : card.shape === 'matches' ? card.groups.length : card.paths.length
  const unit = countsMatches ? '处匹配' : card.shape === 'matches' ? '个文件' : '个路径'
  return (
    <CardShell
      label="搜索结果"
      title={card.shape === 'matches' ? '按内容匹配' : '按路径匹配'}
      meta={`显示 ${String(shown)} ${unit} / 共 ${String(card.total)}${countsMatches ? ` 处匹配 · ${String(card.groups.length)} 个文件` : ''}`}
    >
      {card.shape === 'matches' ? (
        <ul className={styles.groups}>
          {card.groups.map((group) => (
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
          {card.paths.map((path) => (
            <li key={path} className={styles.pathRow}>
              {path}
            </li>
          ))}
        </ul>
      )}
      {card.truncated ? (
        <p className={styles.note}>
          结果已截断，仅显示前 {String(shown)} {unit}。可缩小范围后继续搜索。
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
          {card.stdout === undefined ? null : <pre className={styles.terminal}>{card.stdout}</pre>}
          {card.stderr === undefined ? null : (
            <>
              <p className={styles.note}>标准错误输出：</p>
              <pre className={styles.terminal}>{card.stderr}</pre>
            </>
          )}
        </>
      ) : (
        <pre className={styles.terminal}>{card.output}</pre>
      )}
      {card.timedOut === true ? (
        <p className={styles.note}>命令超时，已停止执行。</p>
      ) : null}
      {card.collapsedLines > 0 ? (
        <p className={styles.note}>
          输出较长，当前省略了 {String(card.collapsedLines)} 行。
        </p>
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
  return (
    <CardShell
      label="工具结果"
      title={card.target === '' ? '工具结果' : card.target}
      meta={card.summary}
    >
      {card.body === '' ? null : <pre className={styles.terminal}>{card.body}</pre>}
      {card.truncated ? (
        <p className={styles.note}>内容较长，当前只显示部分结果。</p>
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

function GenericCard({ card }: { card: Extract<ToolCardView, { kind: 'generic' }> }) {
  return (
    <CardShell label="工具结果" title={card.headline} meta="结果详情">
      <p className={styles.generic}>{card.detail}</p>
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
    case 'generic':
      return <GenericCard card={card} />
    /* v8 ignore next 2 -- file mutations render through DiffCard in ToolRow */
    case 'file-mutation':
      return <GenericCard card={{ kind: 'generic', headline: card.diff.path, detail: '见完整 diff 卡' }} />
  }
}
