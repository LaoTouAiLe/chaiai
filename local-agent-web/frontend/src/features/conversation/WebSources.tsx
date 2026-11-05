import { useId, useState } from 'react'
import type { MessageView, WebSourceView } from '../../fixtures/types'
import styles from './WebSources.module.css'

function keyOf(value: string): string | null {
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol)) return null
    url.hash = ''
    return url.href.replace(/\/$/, '')
  } catch { return null }
}

/** 只从本轮成功工具的结构化来源取值；回答中凭空出现的网址不会变成来源。 */
function sourcesForTurn(rows: readonly MessageView[]): WebSourceView[] {
  const sources = new Map<string, WebSourceView>()
  for (const row of rows) {
    if (row.kind !== 'tool' || row.tool.status !== 'succeeded') continue
    for (const source of row.tool.webSources ?? []) {
      const key = keyOf(source.url)
      if (key === null) continue
      const previous = sources.get(key)
      if (previous === undefined) sources.set(key, source)
      else if (source.kind === 'read') sources.set(key, { ...source, cited: source.cited === undefined && previous.cited === undefined ? undefined : Boolean(source.cited || previous.cited) })
      else if (source.cited) sources.set(key, { ...previous, cited: true })
    }
  }
  return [...sources.values()]
}

export function WebSources({ rows, answer }: { rows: readonly MessageView[]; answer: string }) {
  const [expanded, setExpanded] = useState(false)
  const listId = useId()
  const sources = sourcesForTurn(rows)
  if (sources.length === 0) return null
  const cited = new Set((answer.match(/https?:\/\/[^\s<>\])"'`]+/g) ?? [])
    .map(url => keyOf(url.replace(/[，。；！？]+$/, ''))).filter(key => key !== null))
  return (
    <section className={styles.root} aria-label="联网来源">
      <div className={styles.head}><h3>联网来源 · {sources.length}</h3>
        {sources.length > 3 ? <button type="button" aria-expanded={expanded} aria-controls={listId} onClick={() => setExpanded(value => !value)}>{expanded ? '收起来源' : `查看全部 ${sources.length} 个来源`}</button> : null}
      </div>
      <ul id={listId}>
        {(expanded ? sources : sources.slice(0, 3)).map(source => {
          const date = source.retrievedAt ? new Date(source.retrievedAt) : null
          return (
            <li key={source.url}>
              <a href={source.url} target="_blank" rel="noreferrer noopener" title={source.title}>{source.title}</a>
              <span>{(source.cited ?? cited.has(keyOf(source.url) ?? '')) ? '回答引用 · ' : ''}{source.kind === 'read' ? '已读取' : '搜索结果'}</span>
              <small>{new URL(source.url).hostname}</small>
              {source.provider ? <small>检索：{source.provider}</small> : null}
              {source.publishedAt ? <small>发布时间：{source.publishedAt}</small> : null}
              {date !== null && !Number.isNaN(date.getTime())
                ? <time dateTime={source.retrievedAt ?? undefined}>获取于 {date.toLocaleString('zh-CN', { hour12: false })}</time> : null}
            </li>
          )
        })}
      </ul>
      <p>“搜索结果”表示检索命中；“已读取”表示已获取正文。</p>
    </section>
  )
}
