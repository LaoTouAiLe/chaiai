import { Check, ChevronRight, Cross, Doc, Pencil } from '../../components/icons'
import type { ToolRowView } from '../../fixtures/types'
import { DiffCard } from './DiffCard'
import { ToolCard } from './ToolCards'
import styles from './ToolRow.module.css'

interface ToolRowProps {
  tool: ToolRowView
  /**
   * Real wiring (T31/T32). **Omitting it means prototype behaviour**: the row is a
   * static summary whose `expanded` flag comes straight from the fixture, so the
   * static baseline cannot drift.
   */
  onToggle?: () => void
}

const STATUS_CLASS: Record<ToolRowView['status'], string> = {
  running: styles.statusRunning ?? '',
  succeeded: styles.statusSucceeded ?? '',
  failed: styles.statusFailed ?? '',
}

const STATUS_LABEL: Record<ToolRowView['status'], string> = {
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
}

function StatusIcon({ status }: { status: ToolRowView['status'] }) {
  if (status === 'succeeded') return <Check />
  if (status === 'failed') return <Cross />
  return <span className={styles.pulseDot} aria-hidden="true" />
}

function ToolIcon({ name }: { name: string }) {
  if (name === 'edit_file' || name === 'write_file') return <Pencil />
  return <Doc />
}

/**
 * One tool call in the conversation flow: a compact row that expands into the
 * card the call produced.
 *
 * The card is chosen by the payload's own `kind`, so an unknown tool can only
 * reach the generic renderer.
 */
export function ToolRow({ tool, onToggle }: ToolRowProps) {
  return (
    <div className={styles.wrapper}>
      <button type="button" className={styles.row} aria-expanded={tool.expanded} onClick={onToggle}>
        <span className={`${styles.status} ${STATUS_CLASS[tool.status]}`}>
          <StatusIcon status={tool.status} />
          <span className="visually-hidden">{STATUS_LABEL[tool.status]}</span>
        </span>
        <span className={styles.chevron} aria-hidden="true">
          <ChevronRight />
        </span>
        <span className={styles.icon} aria-hidden="true">
          <ToolIcon name={tool.name} />
        </span>
        <span className={styles.name}>{tool.name}</span>
        <span className={styles.summary}>{tool.summary}</span>
        {tool.target === null ? null : <span className={styles.target}>{tool.target}</span>}
        <span className={styles.duration}>{tool.durationLabel}</span>
      </button>

      {tool.expanded ? (
        <>
          {tool.card.kind === 'file-mutation' ? (
            <DiffCard diff={tool.card.diff} />
          ) : (
            <ToolCard card={tool.card} />
          )}
          {tool.evidence == null ? null : (
            <p className={styles.evidence}>
              证据（{tool.evidence.evidenceType}）：{tool.evidence.summary}
              {tool.evidence.verified ? ' · 已机械校验' : ' · 未通过机械校验'}
            </p>
          )}
        </>
      ) : null}
    </div>
  )
}
