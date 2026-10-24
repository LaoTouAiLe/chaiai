// 「滑到最底」箭头（2026-10-19 起用于设置，2026-10-21 提到 components/ 给会话复用）。
//
// 三条口径：
// 1. **只在没到底时出现**：读的是滚动容器自己的尺寸，不认识任何业务字段；
// 2. 内容异步加载会改变高度，光靠 `scroll` 事件判断不到 —— 所以同时监听 DOM 变化与尺寸变化；
// 3. 位置由调用方给：设置里要避开吸底操作栏，会话里要贴着自己那块滚动区。

import { useCallback, useEffect, useState } from 'react'
import type { RefObject } from 'react'

import { ChevronDown } from './icons'
import styles from './ScrollToBottom.module.css'

interface ScrollToBottomProps {
  paneRef: RefObject<HTMLDivElement | null>
  /** 分区 / 会话标识：切换之后重新判断（新内容可能本来就不到一屏）。 */
  resetKey: string
  /** 距容器底部的像素：设置里让开吸底操作栏，会话里紧贴滚动区底部。 */
  offsetBottom?: number
}

export function ScrollToBottom({ paneRef, resetKey, offsetBottom }: ScrollToBottomProps) {
  const [visible, setVisible] = useState(false)

  const measure = useCallback(() => {
    const pane = paneRef.current
    if (pane === null) return
    const max = pane.scrollHeight - pane.clientHeight
    // 24px 容差：滚动条像素取整、行高不等于整数时，差一两个像素不该让按钮闪。
    setVisible(max - pane.scrollTop > 24)
  }, [paneRef])

  useEffect(() => {
    const pane = paneRef.current
    if (pane === null) return undefined
    let frame = 0
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
    }
    measure()
    const observer = new MutationObserver(schedule)
    observer.observe(pane, { childList: true, subtree: true })
    const resizer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    resizer?.observe(pane)
    pane.addEventListener('scroll', schedule, { passive: true })
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      resizer?.disconnect()
      pane.removeEventListener('scroll', schedule)
    }
  }, [measure, paneRef, resetKey])

  if (!visible) return null
  return (
    <button
      type="button"
      className={styles.jumpBottom}
      aria-label="滑到最底"
      title="滑到最底"
      style={offsetBottom === undefined ? undefined : { bottom: offsetBottom }}
      onClick={() => {
        const pane = paneRef.current
        if (pane === null) return
        pane.scrollTo({ top: pane.scrollHeight, behavior: 'smooth' })
      }}
    >
      <ChevronDown size={14} />
    </button>
  )
}
