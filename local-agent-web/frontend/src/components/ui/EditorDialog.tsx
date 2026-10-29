import { useEffect, useId, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Close } from '../icons'
import { useBodyScrollLock } from '../useBodyScrollLock'
import styles from './EditorDialog.module.css'

interface EditorDialogProps {
  title: string
  description?: string
  children: ReactNode
  onClose: () => void
  dismissible?: boolean
}

const FOCUSABLE = 'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]'

/** Editors live outside the page flow; opening and closing never scrolls their source list. */
export function EditorDialog({ title, description, children, onClose, dismissible = true }: EditorDialogProps) {
  const titleId = useId()
  const descriptionId = useId()
  const dialog = useRef<HTMLDivElement>(null)
  useBodyScrollLock()

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const first = dialog.current?.querySelector<HTMLElement>('input:not(:disabled), textarea:not(:disabled), select:not(:disabled)')
    const target = first ?? dialog.current
    target?.focus({ preventScroll: true })
    return () => { if (previous?.isConnected) previous.focus({ preventScroll: true }) }
  }, [])

  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      const dialogs = document.querySelectorAll('[data-editor-dialog]')
      if (dialogs[dialogs.length - 1] !== dialog.current) return
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopImmediatePropagation()
        if (dismissible) onClose()
      }
      if (event.key === 'Tab') {
        const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>('*') ?? [])
          .filter(item => item.matches(FOCUSABLE) && !item.closest('[hidden], [inert]') && getComputedStyle(item).display !== 'none')
        const first = items[0]
        const last = items.at(-1)
        if (!first) { event.preventDefault(); dialog.current?.focus(); return }
        const outside = !dialog.current?.contains(document.activeElement) || document.activeElement === dialog.current
        if (event.shiftKey && (document.activeElement === first || outside)) {
          event.preventDefault(); last?.focus()
        } else if (!event.shiftKey && (document.activeElement === last || outside)) {
          event.preventDefault(); first.focus()
        }
      }
    }
    document.addEventListener('keydown', keyboard, true)
    return () => document.removeEventListener('keydown', keyboard, true)
  }, [dismissible, onClose])

  return createPortal(
    <div className={styles.overlay}>
      <div ref={dialog} className={styles.dialog} role="dialog" aria-modal="true" data-editor-dialog
        aria-labelledby={titleId} aria-describedby={description ? descriptionId : undefined} tabIndex={-1}>
        <header className={styles.header}>
          <div><h2 id={titleId}>{title}</h2>{description ? <p id={descriptionId}>{description}</p> : null}</div>
          <button type="button" className={styles.close} aria-label={`关闭${title}`} title="关闭"
            disabled={!dismissible} onClick={onClose}><Close /></button>
        </header>
        <div className={styles.body}>{children}</div>
      </div>
    </div>, document.body,
  )
}
