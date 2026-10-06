import { useState } from 'react'
import { Copy } from '../../components/icons'
import type { FileDiffView } from '../../fixtures/types'
import { copyText } from '../conversation/clipboard'
import styles from './DiffCard.module.css'

interface DiffCardProps {
  diff: FileDiffView
}

const SIGN: Record<'context' | 'add' | 'del', string> = {
  context: ' ',
  add: '+',
  del: '−',
}

/**
 * Complete file diff — **early visual checkpoint 1** of T26.
 *
 * Two decisions worth keeping:
 * - Line-number gutters are sticky inside the horizontally scrolling body, so a
 *   long line scrolls under the text area instead of pushing the numbers out.
 * - Colours come from the diff tokens; nothing here recomputes counts, so the
 *   header can never disagree with the body.
 */
export function DiffCard({ diff }: DiffCardProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copyLabel = copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : `复制 ${diff.path} 的 diff`
  const diffText = [`--- a/${diff.path}`, `+++ b/${diff.path}`, ...diff.hunks.flatMap((hunk) => [
    hunk.header,
    ...hunk.lines.map((line) => `${line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ' '}${line.text}`),
  ])].join('\n')
  return (
    <figure className={styles.card}>
      <figcaption className={styles.header}>
        <span className={styles.path}>{diff.path}</span>
        <span className={styles.stats}>
          <span className={styles.additions}>+{diff.additions}</span>
          <span className={styles.deletions}>−{diff.deletions}</span>
          <span className={styles.fileCount}>· 1 file</span>
        </span>
        <button type="button" className={styles.copy} aria-label={copyLabel} title={copyLabel} onClick={() => {
          void copyText(diffText).then((ok) => setCopyState(ok ? 'copied' : 'failed'))
        }}>
          <Copy />
        </button>
      </figcaption>

      <div className={styles.body}>
        {diff.hunks.map((hunk) => (
          <div key={hunk.header} className={styles.hunk}>
            <div className={styles.hunkHeader}>{hunk.header}</div>
            {hunk.lines.map((line, index) => {
              const rowClass =
                line.kind === 'add'
                  ? `${styles.row} ${styles.rowAdd}`
                  : line.kind === 'del'
                    ? `${styles.row} ${styles.rowDel}`
                    : styles.row
              return (
                <div key={`${hunk.header}:${String(index)}`} className={rowClass}>
                  <span className={`${styles.gutter} ${styles.gutterOld}`}>{line.oldLine ?? ''}</span>
                  <span className={`${styles.gutter} ${styles.gutterNew}`}>{line.newLine ?? ''}</span>
                  <span className={styles.sign} aria-hidden="true">
                    {SIGN[line.kind]}
                  </span>
                  <span className={styles.code}>{line.text}</span>
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </figure>
  )
}
