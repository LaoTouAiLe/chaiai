import { useEffect, useId, useRef, useState } from 'react'
import { ChevronRight } from '../../components/icons'
import styles from './ReasoningRow.module.css'
import { useGeneralPreferences } from '../settings/preferencesContext'

/** 展示模型实际返回的思考。参考 DSH 的折叠行与运行中末行预览。 */
export function ReasoningRow({ text, running, truncated = false }: {
  text: string; running: boolean; truncated?: boolean
}) {
  const { reasoning_expanded: defaultExpanded } = useGeneralPreferences()
  const [override, setOverride] = useState<boolean | null>(null)
  const expanded = override ?? defaultExpanded
  const summaryRef = useRef<HTMLSpanElement>(null)
  const bodyRef = useRef<HTMLDivElement>(null)
  const followRef = useRef(true)
  const bodyId = useId()
  const visible = text.trimEnd()
  const summary = running ? visible.slice(visible.lastIndexOf('\n') + 1) : text.split('\n')[0]

  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const summaryElement = summaryRef.current
      if (summaryElement !== null) summaryElement.scrollLeft = running
        ? summaryElement.scrollWidth - summaryElement.clientWidth : 0
      const body = bodyRef.current
      if (running && followRef.current && body !== null) body.scrollTop = body.scrollHeight
    })
    return () => cancelAnimationFrame(frame)
  }, [text, running, expanded])

  return (
    <div className={styles.root} data-state={running ? 'running' : 'complete'}>
      <button type="button" className={styles.row} aria-expanded={expanded} aria-controls={bodyId}
        aria-label={running ? 'Think，正在思考' : 'Think'} onClick={() => setOverride(!expanded)}>
        <span className={styles.icon} aria-hidden="true">✧</span>
        <span className={styles.title}>Think</span>
        <span className={styles.chevron} data-open={expanded}><ChevronRight /></span>
        {!expanded ? <span ref={summaryRef} className={styles.preview} data-running={running} aria-hidden="true">{summary}</span> : null}
      </button>
      {expanded ? <div id={bodyId} className={styles.body} role="region" aria-label="思考内容" ref={bodyRef}
        onScroll={(event) => {
          const body = event.currentTarget
          followRef.current = body.scrollHeight - body.scrollTop - body.clientHeight < 40
        }}>{text}</div> : null}
      {truncated ? <span className={styles.limit}>思考内容展示已截断</span> : null}
    </div>
  )
}
