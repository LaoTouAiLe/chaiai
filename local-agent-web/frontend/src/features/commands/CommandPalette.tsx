import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { searchSessions } from '../../api/client'
import styles from './CommandPalette.module.css'

interface PaletteAction {
  id: string
  label: string
  hint?: string
  onSelect: () => void
}

interface Item {
  key: string
  label: string
  hint: string
  run: () => void
}

/**
 * 命令面板（2026-10-22 V7，规范 §5.7）。
 *
 * 两组结果：**动作**（本地，纯前端）与**会话**（服务端搜索 §15.11.2）。
 * 会话搜索匹配"标题 + 我问过什么"，所以找旧会话时不必记得标题。
 *
 * 没有 `q` 时只显示动作——不把最近 20 个会话伪装成"搜索结果"。
 */
export function CommandPalette({
  onClose,
  onOpenSession,
  actions,
}: {
  onClose: () => void
  onOpenSession: (sessionId: string) => void
  actions: readonly PaletteAction[]
}) {
  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [index, setIndex] = useState(0)
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    input.current?.focus()
  }, [])

  // 250ms 防抖：连打时只发最后一次，避免每个字符一个请求。
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [query])

  const search = useQuery({
    queryKey: ['session-search', debounced],
    queryFn: () => searchSessions(debounced, 20),
    enabled: debounced.length > 0,
    staleTime: 10_000,
  })

  const items = useMemo<Item[]>(() => {
    const needle = query.trim().toLocaleLowerCase()
    const matched = actions
      .filter((action) => needle === '' || action.label.toLocaleLowerCase().includes(needle))
      .map((action) => ({
        key: `action:${action.id}`,
        label: action.label,
        hint: action.hint ?? '动作',
        run: action.onSelect,
      }))
    const sessions = (search.data ?? []).map((session) => ({
      key: `session:${session.session_id}`,
      label: session.title || '未命名会话',
      hint: session.workspace?.display_name ?? '会话',
      run: () => onOpenSession(session.session_id),
    }))
    return [...matched, ...sessions]
  }, [actions, onOpenSession, query, search.data])

  // 结果集变化后把选中项夹回范围内，避免停在已消失的那一行。
  useEffect(() => {
    setIndex((current) => Math.min(current, Math.max(0, items.length - 1)))
  }, [items.length])

  const run = (item: Item | undefined) => {
    if (item === undefined) return
    onClose()
    item.run()
  }

  return (
    <div className={styles.overlay} role="presentation" onMouseDown={onClose}>
      <div
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.stopPropagation()
            onClose()
            return
          }
          if (event.key === 'ArrowDown') {
            event.preventDefault()
            setIndex((current) => Math.min(current + 1, items.length - 1))
          }
          if (event.key === 'ArrowUp') {
            event.preventDefault()
            setIndex((current) => Math.max(current - 1, 0))
          }
          if (event.key === 'Enter') {
            event.preventDefault()
            run(items[index])
          }
        }}
      >
        <input
          ref={input}
          className={styles.input}
          value={query}
          placeholder="搜索会话、动作…"
          aria-label="搜索会话与动作"
          onChange={(event) => {
            setQuery(event.target.value)
            setIndex(0)
          }}
        />
        <ul className={styles.list} role="listbox" aria-label="结果">
          {items.map((item, position) => (
            <li key={item.key}>
              <button
                type="button"
                role="option"
                aria-selected={position === index}
                className={position === index ? `${styles.row} ${styles.rowActive}` : styles.row}
                onMouseEnter={() => setIndex(position)}
                onClick={() => run(item)}
              >
                <span className={styles.label}>{item.label}</span>
                <span className={styles.hint}>{item.hint}</span>
              </button>
            </li>
          ))}
        </ul>
        {items.length === 0 ? (
          <p className={styles.empty} role="status">
            {debounced === '' ? '没有匹配的动作。' : search.isFetching ? '正在搜索…' : '没有匹配的会话或动作。'}
          </p>
        ) : null}
        <p className={styles.footnote}>
          ↑↓ 选择 · Enter 打开 · Esc 关闭
          {debounced === '' ? ' · 输入关键词可搜索历史会话（标题与你的提问）' : ''}
        </p>
      </div>
    </div>
  )
}
