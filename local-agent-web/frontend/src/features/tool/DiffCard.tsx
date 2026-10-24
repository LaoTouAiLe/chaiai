import { useEffect, useMemo, useState } from 'react'
import { Copy } from '../../components/icons'
import type { DiffLineView, FileDiffView } from '../../fixtures/types'
import { copyText } from '../conversation/clipboard'
import { highlightLines, isDarkTheme, languageOf } from './highlight'
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
 * 单个 hunk 默认渲染多少行（2026-10-22）。
 *
 * 为什么要有这个数：整段重写时一个 hunk 可能是 1000+ 行，一次性挂满 DOM 既卡滚动，
 * 也让用户以为"整个文件都变了"。默认只给前 200 行，其余折叠成一行按钮——
 * 想看完的可以展开，不想看的不会被淹没。
 */
const HUNK_LINE_LIMIT = 200

type RenderRow =
  | { kind: 'gap' }
  | { kind: 'line'; line: DiffLineView }
  | { kind: 'more'; hunkIndex: number; hidden: number }

/**
 * 完整文件 diff。视觉与结构按 Codex 的 `diff_render` 对齐（2026-10-21）：
 *
 * - 一行一个文件头：`path (+N -M)`，无卡片边框、无灰底；
 * - **单列行号**：新增行给新行号、删除行给旧行号、上下文给新行号；
 * - 行号后是 gutter 符号（`+` / `-` / 空格），再是正文；
 * - hunk 之间用 `⋮` 分隔，而不是把 `@@` 头堆在正文里。
 *
 * 语法高亮（2026-10-22）：shiki 按需加载，认不出的语言/加载失败都退回纯文本；
 * 着色只影响正文字色，行背景仍由 diff 的加/删色决定。
 */
export function DiffCard({ diff }: DiffCardProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  // 哪些 hunk 被用户点开过（长 hunk 只给前 200 行，其余折叠）。
  const [expandedHunks, setExpandedHunks] = useState<ReadonlySet<number>>(() => new Set())
  const [highlight, setHighlight] = useState<string[][] | null>(null)

  const copyLabel = copyState === 'copied' ? '已复制' : copyState === 'failed' ? '复制失败' : `复制 ${diff.path} 的 diff`
  const diffText = [`--- a/${diff.path}`, `+++ b/${diff.path}`, ...diff.hunks.flatMap((hunk) => [
    hunk.header,
    ...hunk.lines.map((line) => `${line.kind === 'add' ? '+' : line.kind === 'del' ? '-' : ' '}${line.text}`),
  ])].join('\n')

  // 行号列宽度按整个 diff 的最大行号定，避免不同 hunk 之间数字跳动。
  const maxLine = diff.hunks.reduce(
    (max, hunk) => hunk.lines.reduce(
      (inner, line) => Math.max(inner, line.oldLine ?? 0, line.newLine ?? 0),
      max,
    ),
    0,
  )
  const numWidth = `${String(Math.max(1, maxLine)).length + 1}ch`

  // 把 hunk 拍平成渲染行：`⋮` 与"展开"都占一行，保证下标与高亮结果一一对应。
  const rows = useMemo(() => {
    const items: RenderRow[] = []
    diff.hunks.forEach((hunk, hunkIndex) => {
      if (hunkIndex > 0) items.push({ kind: 'gap' })
      const visible = expandedHunks.has(hunkIndex) ? hunk.lines : hunk.lines.slice(0, HUNK_LINE_LIMIT)
      for (const line of visible) items.push({ kind: 'line', line })
      if (visible.length < hunk.lines.length) {
        items.push({ kind: 'more', hunkIndex, hidden: hunk.lines.length - visible.length })
      }
    })
    return items
  }, [diff.hunks, expandedHunks])

  const lang = useMemo(() => languageOf(diff.path), [diff.path])

  useEffect(() => {
    if (lang === null) {
      setHighlight(null)
      return undefined
    }
    let cancelled = false
    const run = () => {
      const code = rows.map((row) => (row.kind === 'line' ? row.line.text : '')).join('\n')
      void highlightLines(code, lang, isDarkTheme()).then((result) => {
        if (!cancelled) setHighlight(result)
      })
    }
    run()
    // 主题切换改的是 `data-theme`，不会触发 React 渲染，只能靠观察者重算颜色。
    const observer = new MutationObserver(run)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => {
      cancelled = true
      observer.disconnect()
    }
  }, [rows, lang])

  return (
    <figure className={styles.card}>
      <figcaption className={styles.header}>
        <span className={styles.path} title={diff.path}>{diff.path}</span>
        <span className={styles.stats}>
          <span className={styles.additions}>+{diff.additions}</span>
          <span className={styles.deletions}>−{diff.deletions}</span>
        </span>
        <button type="button" className={styles.copy} aria-label={copyLabel} title={copyLabel} onClick={() => {
          void copyText(diffText).then((ok) => setCopyState(ok ? 'copied' : 'failed'))
        }}>
          <Copy />
        </button>
      </figcaption>

      <div className={styles.body}>
        {rows.map((row, index) => {
          if (row.kind === 'gap') {
            return (
              <div key={`gap:${String(index)}`} className={styles.gap} aria-hidden="true">
                <span className={styles.num} style={{ width: numWidth }} />
                <span className={styles.sign}>⋮</span>
              </div>
            )
          }
          if (row.kind === 'more') {
            return (
              <button
                key={`more:${String(index)}`}
                type="button"
                className={styles.more}
                onClick={() => setExpandedHunks((prev) => {
                  const next = new Set(prev)
                  next.add(row.hunkIndex)
                  return next
                })}
              >
                还有 {row.hidden} 行 · 展开
              </button>
            )
          }
          const { line } = row
          const rowClass =
            line.kind === 'add'
              ? `${styles.row} ${styles.rowAdd}`
              : line.kind === 'del'
                ? `${styles.row} ${styles.rowDel}`
                : styles.row
          // 单列行号：新增行用新行号、删除行用旧行号、上下文用新行号（Codex 同口径）。
          const number = line.kind === 'del' ? line.oldLine : line.newLine
          const html = highlight?.[index]?.join('') ?? null
          return (
            <div key={`line:${String(index)}`} className={rowClass}>
              <span className={styles.num} style={{ width: numWidth }}>{number ?? ''}</span>
              <span className={styles.sign} aria-hidden="true">{SIGN[line.kind]}</span>
              {html === null
                ? <span className={styles.code}>{line.text}</span>
                : <span className={styles.code} dangerouslySetInnerHTML={{ __html: html }} />}
            </div>
          )
        })}
      </div>
    </figure>
  )
}
