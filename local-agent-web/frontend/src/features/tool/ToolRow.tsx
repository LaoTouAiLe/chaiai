import { Check, ChevronRight, Cross, Doc, Pencil } from '../../components/icons'
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
}

const STATUS_CLASS: Record<ToolRowView['status'], string> = {
  pending: styles.statusPending ?? '',
  running: styles.statusRunning ?? '',
  succeeded: styles.statusSucceeded ?? '',
  failed: styles.statusFailed ?? '',
}

function StatusIcon({ status }: { status: ToolRowView['status'] }) {
  if (status === 'pending') return <span className={styles.pendingDot} aria-hidden="true" />
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
    <div className={styles.wrapper} data-state={tool.status}>
      <button type="button" className={styles.row} aria-expanded={tool.expanded} onClick={onToggle}>
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
              验证依据：{tool.evidence.summary}
              {tool.evidence.verified ? ' · 已机械校验' : ' · 未通过机械校验'}
            </p>
          )}
        </>
      ) : null}
    </div>
  )
}
