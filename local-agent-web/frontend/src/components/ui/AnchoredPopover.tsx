import { useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import styles from './AnchoredPopover.module.css'

interface AnchoredPopoverProps {
  anchorRef?: RefObject<HTMLElement | null>
  label: string
  children: ReactNode
  onClose?: () => void
}

/** Fixed to its trigger rather than participating in the composer or conversation layout. */
export function AnchoredPopover({ anchorRef, label, children, onClose }: AnchoredPopoverProps) {
  const panel = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  useLayoutEffect(() => { close.current = onClose }, [onClose])
  useLayoutEffect(() => {
    const element = panel.current
    if (!element) return
    const anchor = anchorRef?.current
    const position = () => {
      const margin = 12
      const gap = 8
      const rect = anchor?.getBoundingClientRect()
      const width = Math.min(420, window.innerWidth - margin * 2)
      const above = rect ? rect.top - margin - gap : window.innerHeight - margin * 2
      const below = rect ? window.innerHeight - rect.bottom - margin - gap : 0
      const upwards = above >= Math.min(320, below)
      element.style.width = `${width}px`
      element.style.maxHeight = `${Math.max(80, Math.min(480, upwards ? above : below))}px`
      element.style.left = `${Math.max(margin, Math.min(rect?.left ?? margin, window.innerWidth - margin - width))}px`
      element.style.top = `${upwards ? Math.max(margin, (rect?.top ?? window.innerHeight - margin) - gap - element.offsetHeight) : (rect?.bottom ?? margin) + gap}px`
      element.style.visibility = 'visible'
    }
    position()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(position)
    observer?.observe(element)
    if (anchor) observer?.observe(anchor)
    window.addEventListener('resize', position)
    window.addEventListener('scroll', position, true)
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !element.contains(event.target) && !anchor?.contains(event.target)) close.current?.()
    }
    const focusOutside = (event: FocusEvent) => {
      if (event.target instanceof Node && !element.contains(event.target) && !anchor?.contains(event.target)) close.current?.()
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', focusOutside)
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !document.querySelector('[data-editor-dialog]')) {
        event.preventDefault(); event.stopImmediatePropagation(); close.current?.()
      }
    }
    document.addEventListener('keydown', keyboard, true)
    // Pure fixtures have no actions. Live popovers focus the current choice for keyboard access.
    if (close.current) (element.querySelector<HTMLElement>('[aria-pressed="true"]:not(:disabled)') ?? element).focus({ preventScroll: true })
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', position)
      window.removeEventListener('scroll', position, true)
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('focusin', focusOutside)
      document.removeEventListener('keydown', keyboard, true)
      if (element.contains(document.activeElement) && anchor?.isConnected) anchor.focus({ preventScroll: true })
    }
  }, [anchorRef])

  return createPortal(<div ref={panel} className={styles.popover} role="dialog" aria-label={label} tabIndex={-1}>
    {children}</div>, document.body)
}
