// 对话流里的一轮改动摘要（2026-10-21；2026-10-23 按参考图改成卡片 + 悬浮 diff）。
//
// 交互按用户口径（2026-10-23）：
// - 卡片头行：文件图标 + `编辑了 N 个文件` + 「查看变更」；
// - **悬停**文件行 → 浮层里预览这个文件的 diff（只读、不吃鼠标）；
// - **点击**文件行 → 仍打开右栏的改动页，与以前一致。
// 刻意没有「撤销」：后端目前只有会话级 restore，没有还原文件的接口，不做假按钮。
// 计数口径仍是**累计**（每次改动的行数相加），与右栏的"净变化"不是一回事，所以标题旁保留标注。

import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { useSessionChangeDiff, useSessionChanges } from '../../api/queries'
import type { DiffLineView, FileDiffView } from '../../fixtures/types'
import { splitPath, toFileDiffView, toTurnChanges } from './adapters'
import styles from './TurnChanges.module.css'

/** 最多渲染的条目数：超出只报数量，不无限往下堆。 */
const MAX_ROWS = 20

/** 悬浮预览最多渲染多少行（含 hunk 之间的分隔行）；浮层不吃鼠标，所以不能靠滚动看剩下的。 */
const PREVIEW_LINES = 12

/** 悬停多久才弹：鼠标扫过一行时不该闪一下。 */
const HOVER_DELAY = 140

const POPOVER_WIDTH = 560
const POPOVER_MAX_HEIGHT = 300
/** 浮层下方留不到这么多空间就翻到行上方去。 */
const POPOVER_MIN_SPACE = 160
const VIEWPORT_GAP = 12

interface TurnChangesProps {
  sessionId: string | null
  turnId: string
  selectedPath: string | null
  onSelect: (path: string) => void
}

/** 就地预览的一行：要么是 diff 行，要么是 hunk 之间的分隔。 */
type PreviewRow = { kind: 'gap'; key: string } | { kind: 'line'; key: string; line: DiffLineView }

/** 浮层锚点：视口坐标（`position: fixed`），同一份状态里带上是哪个文件。 */
interface PopoverAnchor {
  path: string
  left: number
  width: number
  top: number | null
  bottom: number | null
  maxHeight: number
}

function FileGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      strokeWidth="1.2" aria-hidden="true">
      <path d="M4 2.5h5l3 3v8H4z" strokeLinejoin="round" />
      <path d="M9 2.5v3h3" strokeLinejoin="round" />
    </svg>
  )
}

/**
 * 按行的位置算浮层摆放：优先贴在行下方，下方空间不够就翻到行上方。
 *
 * 不在渲染后再测量高度——浮层是 `pointer-events: none` 的纯展示，翻不翻只看**可用空间**，
 * 提前算好可以避免"先闪一下再跳位"。
 */
function anchorAt(rect: DOMRect, path: string): PopoverAnchor {
  const width = Math.min(POPOVER_WIDTH, window.innerWidth - VIEWPORT_GAP * 2)
  const left = Math.max(VIEWPORT_GAP, Math.min(rect.left, window.innerWidth - width - VIEWPORT_GAP))
  const spaceBelow = window.innerHeight - rect.bottom - VIEWPORT_GAP
  const spaceAbove = rect.top - VIEWPORT_GAP
  const below = spaceBelow >= POPOVER_MIN_SPACE || spaceBelow >= spaceAbove
  const space = below ? spaceBelow : spaceAbove
  return {
    path,
    left,
    width,
    maxHeight: Math.max(120, Math.min(POPOVER_MAX_HEIGHT, space)),
    top: below ? rect.bottom + 6 : null,
    bottom: below ? null : window.innerHeight - rect.top + 6,
  }
}

const SIGN = { context: ' ', add: '+', del: '−' } as const

/** diff 行（含 hunk 分隔）：浮层里的小预览与右栏的 `DiffCard` 同一套红绿底色，但不做语法高亮。 */
function DiffLines({ diff }: { diff: FileDiffView }) {
  // hunk 之间插一条分隔行（`⋮`）：行号在这里跳了，不说一声会让人以为漏读了。
  const rows: PreviewRow[] = []
  diff.hunks.forEach((hunk, hunkIndex) => {
    if (hunkIndex > 0) rows.push({ kind: 'gap', key: `gap:${String(hunkIndex)}` })
    hunk.lines.forEach((line, lineIndex) => {
      rows.push({ kind: 'line', key: `${String(hunkIndex)}:${String(lineIndex)}`, line })
    })
  })
  const shownRows = rows.slice(0, PREVIEW_LINES)
  const hidden = rows.length - shownRows.length
  const maxLine = diff.hunks.reduce(
    (max, hunk) => hunk.lines.reduce((inner, line) => Math.max(inner, line.oldLine ?? 0, line.newLine ?? 0), max),
    0,
  )
  const numWidth = `${String(Math.max(1, maxLine)).length + 1}ch`

  return (
    <>
      <div className={styles.popoverBody}>
        {shownRows.map((row) => {
          if (row.kind === 'gap') {
            return (
              <div key={row.key} className={styles.pGap} aria-hidden="true">
                <span className={styles.pNum} style={{ width: numWidth }} />
                <span className={styles.pSign}>⋮</span>
              </div>
            )
          }
          const { line } = row
          const rowClass = line.kind === 'add'
            ? `${styles.pRow} ${styles.pAdd}`
            : line.kind === 'del'
              ? `${styles.pRow} ${styles.pDel}`
              : styles.pRow
          const number = line.kind === 'del' ? line.oldLine : line.newLine
          return (
            <div key={row.key} className={rowClass}>
              <span className={styles.pNum} style={{ width: numWidth }}>{number ?? ''}</span>
              <span className={styles.pSign} aria-hidden="true">{SIGN[line.kind]}</span>
              <span className={styles.pCode}>{line.text}</span>
            </div>
          )
        })}
      </div>
      {hidden > 0 ? (
        <p className={styles.popoverNote}>还有 {hidden} 行未显示 · 点击文件看完整 diff</p>
      ) : null}
    </>
  )
}

/**
 * 悬浮预览的内容。
 *
 * 数据在**悬停时才取**（`useSessionChangeDiff` 按文件缓存，重复悬停不会重复发请求）；
 * 取不到改前内容时只给一句原因，不假装有 diff。
 */
function DiffPreview({ sessionId, path, unavailableReason }: {
  sessionId: string | null
  path: string
  unavailableReason: string | null
}) {
  const query = useSessionChangeDiff(sessionId, path)
  const data = query.data

  let body: ReactNode
  if (query.isLoading) {
    body = <p className={styles.popoverNote}>正在读取这个文件的改前内容…</p>
  } else if (query.isError) {
    body = <p className={styles.popoverNote} role="alert">这个文件的变更暂时读不到，请稍后重试。</p>
  } else if (data === undefined) {
    body = null
  } else if (!data.has_baseline) {
    body = (
      <p className={styles.popoverNote}>
        {data.unavailable_reason ?? unavailableReason ?? '这个文件没有记录改前内容，无法给出前后对比。'}
      </p>
    )
  } else if ((data.hunks ?? []).length === 0) {
    body = <p className={styles.popoverNote}>当前文件与该会话开始前没有差异（可能已被还原）。</p>
  } else {
    body = <DiffLines diff={toFileDiffView(data)} />
  }

  return (
    <>
      <div className={styles.popoverHead}>
        <span className={styles.popoverPath} title={path}>{path}</span>
        {/* 这里的 +N −M 用 diff 自己的数（净变化），与浮层里这几行同一个口径。 */}
        {data === undefined ? null : (
          <span className={styles.popoverStats}>
            <span className={styles.add}>+{data.additions}</span>
            <span className={styles.del}>−{data.deletions}</span>
          </span>
        )}
      </div>
      {body}
    </>
  )
}

export function TurnChanges({ sessionId, turnId, selectedPath, onSelect }: TurnChangesProps) {
  const query = useSessionChanges(sessionId, sessionId !== null)
  const [hover, setHover] = useState<PopoverAnchor | null>(null)
  const timer = useRef<number | null>(null)
  const changes = toTurnChanges(query.data, turnId)

  const cancelHover = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current)
      timer.current = null
    }
  }, [])

  useEffect(() => cancelHover, [cancelHover])

  // 浮层是 `position: fixed`，任何滚动/缩放都会让它和行错位——直接收掉，重新悬停再弹。
  useEffect(() => {
    if (hover === null) return undefined
    const close = (): void => { setHover(null) }
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [hover])

  if (changes === null) return null

  const shown = changes.files.slice(0, MAX_ROWS)
  const hidden = changes.files.length - shown.length
  // 「查看变更」优先看当前选中的那个文件，但它必须属于这一轮——`selectedPath` 可能来自别的轮。
  const target = changes.files.find((file) => file.path === selectedPath)?.path ?? changes.files[0]?.path
  const hovered = hover === null ? undefined : changes.files.find((file) => file.path === hover.path)

  return (
    <div className={styles.card} aria-label="本轮改动的文件">
      <header className={styles.head}>
        <span className={styles.icon}><FileGlyph /></span>
        <span className={styles.title}>编辑了 {changes.files.length} 个文件</span>
        <span className={styles.scope}>累计</span>
        <span className={styles.grow} />
        {target === undefined ? null : (
          <button
            type="button"
            className={styles.link}
            title="在右栏打开这个文件的完整变更"
            onClick={() => { onSelect(target) }}
          >
            查看变更
          </button>
        )}
      </header>

      <ul className={styles.list}>
        {shown.map((file) => {
          const { dir, name } = splitPath(file.path)
          const active = selectedPath === file.path
          return (
            <li key={file.path}>
              <button
                type="button"
                className={active ? `${styles.row} ${styles.rowActive}` : styles.row}
                title={file.path}
                onClick={() => {
                  cancelHover()
                  setHover(null)
                  onSelect(file.path)
                }}
                onMouseEnter={(event) => {
                  const rect = event.currentTarget.getBoundingClientRect()
                  cancelHover()
                  timer.current = window.setTimeout(() => {
                    timer.current = null
                    setHover(anchorAt(rect, file.path))
                  }, HOVER_DELAY)
                }}
                onMouseLeave={() => {
                  cancelHover()
                  setHover(null)
                }}
              >
                <span className={styles.path}>
                  {dir === '' ? null : <span className={styles.dir}>{dir}</span>}
                  <span className={styles.name}>{name}</span>
                </span>
                <span className={styles.counts}>
                  {file.additions === 0 && file.deletions === 0 ? (
                    '已删除'
                  ) : (
                    <>
                      <span className={styles.add}>+{file.additions}</span>
                      <span className={styles.del}>−{file.deletions}</span>
                    </>
                  )}
                </span>
              </button>
            </li>
          )
        })}
      </ul>
      {hidden > 0 ? <p className={styles.more}>还有 {hidden} 个文件未列出</p> : null}

      {/* 浮层挂到 body：卡片有 `overflow: hidden`，留在里面会被裁掉。 */}
      {hover === null || hovered === undefined ? null : createPortal(
        <div
          className={styles.popover}
          role="tooltip"
          style={{
            left: hover.left,
            width: hover.width,
            maxHeight: hover.maxHeight,
            top: hover.top ?? undefined,
            bottom: hover.bottom ?? undefined,
          }}
        >
          <DiffPreview
            sessionId={sessionId}
            path={hovered.path}
            unavailableReason={hovered.unavailableReason}
          />
        </div>,
        document.body,
      )}
    </div>
  )
}
