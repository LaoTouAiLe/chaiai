// 右栏「变更预览」（2026-10-21）：只显示**某一个文件**的变更前 → 变更后。
//
// 列表不在这里——它已经上移到对话区顶部的 `ChangesSummary`。这个面板只回答一个问题：
// "我刚点的那个文件，到底变成了什么样"。视觉沿用 Codex：一行文件头 + 连续的行，
// 不再套第二层卡片边框与标题。

import { useEffect, useMemo } from 'react'

import { useSessionChangeDiff, useSessionChanges } from '../../api/queries'
import { DiffCard } from '../tool/DiffCard'
import { toFileDiffView } from './adapters'
import styles from './ChangesPanel.module.css'

interface ChangesPanelProps {
  sessionId: string | null
  path: string | null
  /** 切到另一个文件（抽屉内的上/下导航用；不关闭面板）。 */
  onSelect: (path: string) => void
  onClose: () => void
}

/**
 * 单文件「改前 → 改后」对比（`DiffCard`：红绿行 + 语法高亮）。
 *
 * 2026-10-22 起也被右栏工作面板复用——右栏一开始自己写了个裸 `<pre>` 显示 git patch，
 * 结果红绿和代码着色全丢了，用户一眼就看出"改动样式全没了"。
 */
export function FileDiff({ sessionId, path }: { sessionId: string; path: string }) {
  const query = useSessionChangeDiff(sessionId, path)
  if (query.isLoading) {
    return <p className={styles.note}>正在读取这个文件的改前内容…</p>
  }
  if (query.isError) {
    return <p className={styles.note} role="alert">这个文件的变更暂时读不到，请稍后重试。</p>
  }
  const data = query.data
  if (data === undefined) return null
  if (!data.has_baseline) {
    return (
      <p className={styles.note}>
        {data.unavailable_reason ?? '这个文件没有记录改前内容，无法给出完整的前后对比。'}
      </p>
    )
  }
  if ((data.hunks ?? []).length === 0) {
    return <p className={styles.note}>当前文件与该会话开始前没有差异（可能已被还原）。</p>
  }
  return (
    <>
      <DiffCard diff={toFileDiffView(data)} />
      {data.truncated ? <p className={styles.note}>改动过大，只显示了前一部分。</p> : null}
    </>
  )
}

/**
 * 账本之外、被命令/代码动过的文件（2026-10-22）。
 *
 * 单独放一行而不是并进上面的列表：这批文件**没有改前原文**，并进去会让用户以为
 * 面板是完整的。列出来是为了"别让人漏看"，不是为了让它看起来像账本的一部分。
 */
function UnrecordedNote({ sessionId }: { sessionId: string }) {
  const query = useSessionChanges(sessionId)
  const items = query.data?.unrecorded ?? []
  const note = query.data?.unrecorded_note ?? null
  if (items.length === 0) {
    return note === null ? null : <p className={styles.unrecorded}>{note}</p>
  }
  const shown = items.slice(0, 5)
  return (
    <div className={styles.unrecorded}>
      <p>
        另有 <strong>{items.length}</strong> 个文件被命令或代码改动，没有逐次改前内容：
      </p>
      <ul>
        {shown.map((item) => <li key={item.path}><code>{item.path}</code></li>)}
        {items.length > shown.length ? <li>…等共 {items.length} 个</li> : null}
      </ul>
    </div>
  )
}

/**
 * 文件上/下导航（2026-10-22）。
 *
 * 以前换文件只能关掉面板再点列表——抽屉覆盖在页面上时，那等于"退出再进来"两次。
 * 顺序沿用 `useSessionChanges` 的返回顺序（最新一轮在前、轮内按首次改动顺序）。
 */
function FileNav({
  sessionId, path, onSelect,
}: { sessionId: string; path: string | null; onSelect: (path: string) => void }) {
  const query = useSessionChanges(sessionId)
  const paths = useMemo(() => {
    const seen = new Set<string>()
    for (const turn of query.data?.turns ?? []) {
      for (const file of turn.files ?? []) seen.add(file.path)
    }
    return [...seen]
  }, [query.data])

  if (path === null || paths.length < 2) return null
  const index = paths.indexOf(path)
  const previous = index > 0 ? paths[index - 1] : null
  const next = index >= 0 && index < paths.length - 1 ? paths[index + 1] : null

  return (
    <span className={styles.nav}>
      <button
        type="button"
        className={styles.navButton}
        disabled={previous === null}
        aria-label="上一个文件"
        title={previous ?? undefined}
        onClick={() => { if (previous !== null) onSelect(previous) }}
      >
        ↑
      </button>
      <span className={styles.navCount}>{index + 1}/{paths.length}</span>
      <button
        type="button"
        className={styles.navButton}
        disabled={next === null}
        aria-label="下一个文件"
        title={next ?? undefined}
        onClick={() => { if (next !== null) onSelect(next) }}
      >
        ↓
      </button>
    </span>
  )
}

export function ChangesPanel({ sessionId, path, onSelect, onClose }: ChangesPanelProps) {
  // Esc 关闭（2026-10-22）：面板改成覆盖式抽屉后，必须有一个不依赖鼠标的退出方式，
  // 否则用户只能去右上角找那个很小的 ×。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey) }
  }, [onClose])

  return (
    <>
      {/* 遮罩：点它等于关闭。抽屉浮在页面上，不再挤压主对话区。 */}
      <div className={styles.backdrop} onClick={onClose} aria-hidden="true" />
      <aside className={styles.panel} role="dialog" aria-modal="true" aria-label="变更预览">
        <header className={styles.header}>
          <span className={styles.label}>
            变更预览
            {/* 口径标注（2026-10-22）：这里是基线 → 当前的**净变化**，与对话流里的"累计"不同。 */}
            <span className={styles.scope}>净变化</span>
            {path === null ? null : <code className={styles.path}>{path}</code>}
          </span>
          {sessionId === null ? null : (
            <FileNav sessionId={sessionId} path={path} onSelect={onSelect} />
          )}
          <button type="button" className={styles.close} aria-label="关闭变更预览" onClick={onClose}>
            ×
          </button>
        </header>

        <div className={styles.body}>
          {sessionId === null || path === null ? (
            <p className={styles.empty}>
              在上方的改动列表里点一个文件，这里显示它这次会话里的变更前与变更后。
            </p>
          ) : (
            <FileDiff sessionId={sessionId} path={path} />
          )}
        </div>
        {sessionId === null ? null : <UnrecordedNote sessionId={sessionId} />}
      </aside>
    </>
  )
}
