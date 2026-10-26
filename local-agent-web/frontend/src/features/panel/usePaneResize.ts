import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'

/** 右侧面板及文件树共用：向左增宽，支持触摸、键盘与拖动中卸载。 */
export function usePaneResize(initial: number, min: number, max: number, reset = initial) {
  const [width, setWidth] = useState(initial)
  const [resizing, setResizing] = useState(false)
  const drag = useRef<{
    x: number; width: number; pointerId: number; userSelect: string; cursor: string
  } | null>(null)
  const frame = useRef(0)
  const clamp = (value: number) => Math.min(max, Math.max(min, value))

  const release = useCallback(() => {
    if (drag.current === null) return
    document.body.style.userSelect = drag.current.userSelect
    document.body.style.cursor = drag.current.cursor
    drag.current = null
  }, [])

  useEffect(() => () => {
    cancelAnimationFrame(frame.current)
    release()
  }, [release])

  const stop = () => { release(); setResizing(false) }
  return {
    width, resizing,
    handle: {
      tabIndex: 0,
      'aria-valuemin': min,
      'aria-valuemax': max,
      'aria-valuenow': width,
      'aria-valuetext': `${String(width)} 像素`,
      onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
        if (!event.isPrimary || event.button !== 0) return
        event.preventDefault()
        event.currentTarget.focus()
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = { x: event.clientX, width, pointerId: event.pointerId,
          userSelect: document.body.style.userSelect, cursor: document.body.style.cursor }
        document.body.style.userSelect = 'none'
        document.body.style.cursor = 'col-resize'
        setResizing(true)
      },
      onPointerMove: (event: PointerEvent<HTMLDivElement>) => {
        if (drag.current === null || drag.current.pointerId !== event.pointerId) return
        const next = clamp(drag.current.width + drag.current.x - event.clientX)
        cancelAnimationFrame(frame.current)
        frame.current = requestAnimationFrame(() => setWidth(next))
      },
      onPointerUp: stop,
      onPointerCancel: stop,
      onLostPointerCapture: stop,
      onDoubleClick: () => setWidth(clamp(reset)),
      onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
        const step = event.shiftKey ? 40 : 16
        const next = event.key === 'ArrowLeft' ? width + step
          : event.key === 'ArrowRight' ? width - step
          : event.key === 'Home' ? min : event.key === 'End' ? max : null
        if (next === null) return
        event.preventDefault()
        setWidth(clamp(next))
      },
    },
  }
}
