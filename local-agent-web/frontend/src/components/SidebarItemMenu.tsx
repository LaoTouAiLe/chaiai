import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ApiError } from '../api/client'
import styles from './Sidebar.module.css'

interface Props {
  title: string
  kind: '会话' | '工作区'
  onRename: (title: string) => Promise<void>
  onRemove: () => Promise<void>
  blocked?: boolean
}

function errorText(error: unknown): string {
  return error instanceof ApiError
    ? `${error.message}${error.hint === null ? '' : ` ${error.hint}`}`
    : '操作未保存，请检查连接后重试。'
}

/** 每行独立菜单；菜单动作不触发行选择，失败时保留输入。 */
export function SidebarItemMenu({ title, kind, onRename, onRemove, blocked = false }: Props) {
  const [open, setOpen] = useState(false)
  const [dialog, setDialog] = useState<'rename' | 'remove' | null>(null)
  const [value, setValue] = useState(title)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus()
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  useEffect(() => {
    if (dialog === null) return
    const previous = trigger.current
    panel.current?.querySelector<HTMLElement>('input, button')?.focus()
    return () => previous?.focus()
  }, [dialog])

  const close = () => { if (!pending) { setDialog(null); setError(null) } }
  const perform = async (action: () => Promise<void>) => {
    if (pending) return
    setPending(true)
    setError(null)
    try {
      await action()
      setDialog(null)
      setOpen(false)
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setPending(false)
    }
  }

  return <div ref={root} className={styles.itemActions} onKeyDown={(event) => {
    if (event.key === 'Escape') { setOpen(false); trigger.current?.focus() }
    if (open && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault()
      const items = [...(root.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)') ?? [])]
      const index = items.indexOf(document.activeElement as HTMLElement)
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
      items[next]?.focus()
    }
  }}>
    <button ref={trigger} type="button" className={styles.moreButton}
      aria-label={`${kind}「${title}」的更多操作`} aria-haspopup="menu" aria-expanded={open}
      onClick={() => { setOpen(!open); setError(null) }}>⋯</button>
    {open ? <div role="menu" aria-label={`${kind}操作`} className={styles.itemMenu}>
      <button type="button" role="menuitem" onClick={() => {
        setValue(title); setDialog('rename'); setOpen(false); setError(null)
      }}>重命名</button>
      <button type="button" role="menuitem" disabled={pending || blocked}
        title={blocked ? '请等待运行结束并完成审批后再操作' : undefined}
        onClick={() => {
          if (kind === '工作区') { setDialog('remove'); setOpen(false); setError(null) }
          else void perform(onRemove)
        }}>{kind === '会话' ? '归档会话' : '移除工作区'}</button>
      {error === null ? null : <p role="alert" className={styles.actionError}>{error}</p>}
    </div> : null}
    {dialog === null ? null : createPortal(
      <div className={styles.dialogBackdrop} onClick={(event) => {
        if (event.target === event.currentTarget) close()
      }}>
        <div ref={panel} role="dialog" aria-modal="true"
          aria-label={dialog === 'rename' ? `重命名${kind}` : '移除工作区'}
          className={styles.managementDialog} onKeyDown={(event) => {
            if (event.key === 'Escape') { event.stopPropagation(); close() }
            if (event.key === 'Tab') {
              const nodes = [...(panel.current?.querySelectorAll<HTMLElement>('input:not(:disabled), button:not(:disabled)') ?? [])]
              const first = nodes[0], last = nodes[nodes.length - 1]
              if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
              else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
            }
          }}>
          <h2>{dialog === 'rename' ? `重命名${kind}` : '移除工作区'}</h2>
          <form onSubmit={(event) => {
            event.preventDefault()
            if (dialog === 'rename' && value.trim() === '') { setError('名称不能为空'); return }
            void perform(dialog === 'rename' ? () => onRename(value) : onRemove)
          }}>
            {dialog === 'rename' ? <label>名称
              <input value={value} maxLength={60} disabled={pending}
                onChange={(event) => setValue(event.target.value)} />
            </label> : <p>移除「{title}」后，文件保留在原目录，历史会话会显示在“未分组会话”。重新添加此目录可恢复分组。</p>}
            {error === null ? null : <p role="alert" className={styles.actionError}>{error}</p>}
            <div className={styles.dialogButtons}>
              <button type="button" disabled={pending} onClick={close}>取消</button>
              <button type="submit" disabled={pending}>{pending ? '正在保存…' : dialog === 'rename' ? '保存' : '确认移除'}</button>
            </div>
          </form>
        </div>
      </div>, document.body,
    )}
  </div>
}
