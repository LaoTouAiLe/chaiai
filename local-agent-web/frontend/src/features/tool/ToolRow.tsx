import { useEffect, useState } from 'react'

import { Check, ChevronRight, Cross, Doc, Pencil, WarningTriangle } from '../../components/icons'
import type { ToolRowView } from '../../fixtures/types'
import { DiffCard } from './DiffCard'
import { ToolCard } from './ToolCards'
import { TOOL_STATUS_LABEL, toolLabel } from './display'
import styles from './ToolRow.module.css'

interface ToolRowProps {
  tool: ToolRowView
  /**
   * Real wiring (T31/T32). **Omitting it means prototype behaviour**: the row is a
   * static summary whose `expanded` flag comes straight from the fixture, so the
   * static baseline cannot drift.
   */
  onToggle?: () => void
  /**
   * 把这条正在跑的命令转入后台（2026-10-21）。返回 `false` = 命令已经结束（或不是能转的调用），
   * 行上如实显示"已结束"，而不是假装成功。
   */
  onDetach?: (commandId: string) => Promise<boolean>
}

const STATUS_CLASS: Record<ToolRowView['status'], string> = {
  pending: styles.statusPending ?? '',
  running: styles.statusRunning ?? '',
  succeeded: styles.statusSucceeded ?? '',
  failed: styles.statusFailed ?? '',
  uncertain: styles.statusFailed ?? '',
}

function StatusIcon({ status }: { status: ToolRowView['status'] }) {
  if (status === 'uncertain') return <WarningTriangle />
  if (status === 'pending') return <span className={styles.pendingDot} aria-hidden="true" />
  if (status === 'succeeded') return <Check />
  if (status === 'failed') return <Cross />
  return <span className={styles.pulseDot} aria-hidden="true" />
}

function ToolIcon({ name }: { name: string }) {
  if (name === 'edit_file' || name === 'write_file') return <Pencil />
  return <Doc />
}

/** 运行中的"已跑多久"：秒级就够，超过一分钟按分秒显示。 */
function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  if (seconds < 60) return `已跑 ${String(seconds)}s`
  const minutes = Math.floor(seconds / 60)
  return `已跑 ${String(minutes)}m${String(seconds % 60).padStart(2, '0')}s`
}

/**
 * 运行中的命令最后一行输出。
 *
 * 折叠行只有一行位置，所以取**最后一条非空行**并截断：用户要的是"它现在在干什么"，
 * 完整输出仍在展开卡与后台命令里。
 */
function lastOutputLine(text: string | undefined): string | null {
  if (text === undefined || text === '') return null
  const lines = text.split(/\r?\n/)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = (lines[index] ?? '').trim()
    if (line !== '') return line.length > 160 ? `${line.slice(0, 160)}…` : line
  }
  return null
}

/**
 * One tool call in the conversation flow: a compact row that expands into the
 * card the call produced.
 *
 * The card is chosen by the payload's own `kind`, so an unknown tool can only
 * reach the generic renderer.
 */
export function ToolRow({ tool, onToggle, onDetach }: ToolRowProps) {
  const [detachState, setDetachState] = useState<'idle' | 'pending' | 'done' | 'failed'>('idle')
  // 运行中每秒刷新一次"已跑多久"：这是用户判断"卡住了没有"的主要依据。
  const [nowTick, setNowTick] = useState(() => Date.now())
  useEffect(() => {
    if (tool.status !== 'running') return undefined
    const timer = window.setInterval(() => { setNowTick(Date.now()) }, 1000)
    return () => { window.clearInterval(timer) }
  }, [tool.status])

  const startedMs = tool.startedAt === undefined ? Number.NaN : Date.parse(tool.startedAt)
  const runningElapsed = tool.status === 'running' && Number.isFinite(startedMs)
    ? formatElapsed(nowTick - startedMs)
    : null
  const elapsedLabel = runningElapsed ?? tool.durationLabel
  const liveLine = tool.status === 'running' && tool.card.kind === 'terminal'
    ? lastOutputLine(tool.card.stdout)
    : null
  const background = tool.card.kind === 'terminal' && tool.card.background === true
  const timedOut = tool.card.kind === 'terminal' && tool.card.timedOut === true
  // 只有"正在跑的 shell 命令"能转后台：别的工具没有进程可分离，转不了就是转不了。
  const canDetach =
    tool.status === 'running' && tool.name === 'run_shell'
    && tool.toolCallId !== undefined && onDetach !== undefined

  const detachLabel =
    detachState === 'pending' ? '转后台…'
      : detachState === 'done' ? '后台运行中'
        : detachState === 'failed' ? '已结束' : '转后台'

  return (
    <div className={styles.wrapper} data-state={tool.status}>
      <button
        type="button"
        className={canDetach ? `${styles.row} ${styles.rowWithAction}` : styles.row}
        aria-expanded={tool.expanded}
        onClick={onToggle}
      >
        <span className={`${styles.status} ${STATUS_CLASS[tool.status]}`}>
          <StatusIcon status={tool.status} />
          <span className="visually-hidden">{TOOL_STATUS_LABEL[tool.status]}</span>
        </span>
        <span className={styles.chevron} aria-hidden="true">
          <ChevronRight />
        </span>
        <span className={styles.icon} aria-hidden="true">
          <ToolIcon name={tool.name} />
        </span>
        <span className={styles.name}>{toolLabel(tool.name)}</span>
        {background ? <span className={`${styles.badge} ${styles.badgeInfo}`}>后台运行中</span> : null}
        {!background && timedOut ? <span className={`${styles.badge} ${styles.badgeWarn}`}>超时</span> : null}
        {tool.target === null ? null : <span className={styles.target}>
          {(tool.name === 'run_shell' || tool.name === 'execute_code') && tool.card.kind === 'terminal'
            ? tool.card.command
            : tool.target}
        </span>}
        {/* 单次改动的行数直接摆在行上（2026-10-21）：`✎ 修改文件 empty.html +3 −1`。
            以前必须点开才知道改了多少，而"这次改了几行"正是用户扫一眼就要的信息。 */}
        {tool.card.kind === 'file-mutation' ? (
          <span className={styles.delta}>
            <span className={styles.add}>+{tool.card.diff.additions}</span>
            <span className={styles.del}>−{tool.card.diff.deletions}</span>
          </span>
        ) : null}
        <span className={styles.duration}>{elapsedLabel}</span>
      </button>

      {/* 运行中的最后一行输出：让用户不用展开就能判断"它现在在干什么"。 */}
      {liveLine === null ? null : (
        <p className={styles.liveLine} title={liveLine}>└ {liveLine}</p>
      )}

      {canDetach ? (
        // 独立按钮（不能嵌在行按钮里）：点它不展开、只发"转后台"。
        <button
          type="button"
          className={styles.detach}
          disabled={detachState !== 'idle'}
          title={detachState === 'failed'
            ? '这条命令已经结束，不需要转后台'
            : '命令继续在后台跑，模型可以先做别的事'}
          onClick={() => {
            if (tool.toolCallId === undefined || onDetach === undefined) return
            setDetachState('pending')
            void onDetach(tool.toolCallId).then((ok) => setDetachState(ok ? 'done' : 'failed'))
          }}
        >
          {detachLabel}
        </button>
      ) : null}

      {tool.expanded ? (
        <>
          {tool.card.kind === 'file-mutation' ? (
            <DiffCard diff={tool.card.diff} />
          ) : (
            <ToolCard card={tool.card} />
          )}
          {tool.evidence == null ? null : (
            <p className={styles.evidence}>
              验证依据：{tool.evidence.summary}
              {/* 说"这个结论能不能当依据"，不说内部评级名（2026-10-15）：`verified` 是反思层
                  用代码核对的结果，但"机械校验"是内部概念，摆给用户看等于让他去理解我们的评级。 */}
              {tool.evidence.verified ? ' · 已核对' : ' · 未核对通过'}
            </p>
          )}
        </>
      ) : null}
    </div>
  )
}
