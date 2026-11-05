import { useEffect, useId, useRef, useState } from 'react'
import { ChevronRight } from '../../components/icons'
import styles from './ReasoningRow.module.css'
import { useGeneralPreferences } from '../settings/preferencesContext'
import { MarkdownMessage } from './MarkdownMessage'

/** 展示供应商实际返回的思考；运行中预览完整段落的首行，避免尾部逐字跳动。 */
export function ReasoningRow({ text, running, truncated = false }: {
  text: string; running: boolean; truncated?: boolean
}) {
  const { reasoning_expanded: defaultExpanded } = useGeneralPreferences()
  const [override, setOverride] = useState<boolean | null>(null)
  const expanded = override ?? defaultExpanded
  const summaryRef = useRef<HTMLSpanElement>(null)
  const bodyId = useId()
  const visible = text.trimEnd()
  // Keep the preview on a completed paragraph/line instead of a rapidly changing token tail.
  const paragraphs = visible.split(/\r?\n\s*\r?\n/)
  const stable = paragraphs.filter((part, index) => index < paragraphs.length - 1 || part.includes('\n')).at(-1)
  const preview = running ? (stable ?? paragraphs[0] ?? '') : visible
  const summary = (preview.split(/\r?\n/).find(line => line.trim() !== '') ?? '').replace(/\*\*|__|`/g, '').replace(/^#+\s*/, '')

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const summaryElement = summaryRef.current
      if (summaryElement !== null) summaryElement.scrollLeft = running
        ? summaryElement.scrollWidth - summaryElement.clientWidth : 0
    })
    return () => cancelAnimationFrame(frame)
  }, [text, running, expanded])

  return (
    <div className={styles.root} data-state={running ? 'running' : 'complete'}>
      <button type="button" className={styles.row} aria-expanded={expanded} aria-controls={bodyId}
        aria-label={running ? '思考中' : '思考'} onClick={() => setOverride(!expanded)}>
        <span className={styles.icon} aria-hidden="true">✧</span>
        {/* 中文界面里只剩这一处英文（2026-10-22）：`Think` 是 Codex 的终端文案，Web 端没必要沿用。 */}
        <span className={styles.title}>{running ? '思考中' : '思考'}</span>
        <span className={styles.chevron} data-open={expanded}><ChevronRight /></span>
        {!expanded ? <span ref={summaryRef} className={styles.preview} data-running={running} aria-hidden="true">{summary}</span> : null}
      </button>
      {expanded ? <div id={bodyId} className={styles.body} role="region" aria-label="思考内容"><MarkdownMessage text={text} compact streaming={running} /></div> : null}
      {truncated ? <span className={styles.limit}>思考内容展示已截断</span> : null}
    </div>
  )
}
