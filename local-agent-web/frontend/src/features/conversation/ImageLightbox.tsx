import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'

import styles from './ImageLightbox.module.css'

/**
 * 放大看一张图（2026-10-16）。
 *
 * 为什么要有它：气泡里的缩略图被限制在 220×160（不然一张竖图能把整屏占掉 ✗），而用户常常要
 * **看清内容**——截图里的小字、UI 细节。缩略图给不了这个，所以点开看原图 ✔。
 *
 * 三条与仓库其它弹层一致的做法（见 `components/SidebarItemMenu`）：
 * ① `createPortal` + backdrop 点击关闭；② `role="dialog"` + `aria-modal` + **Esc 关闭** + Tab 自锁；
 * ③ 关闭后把焦点**还给触发它的那张缩略图**（不回焦点的话键盘用户会掉到页面顶部 ✗）。
 *
 * 图像来源就是那条消息的附件地址：服务端回的是**原始字节** ✔，所以放大看到的是原图，
 * 不是重新编码过的副本 ✔。
 */
export function ImageLightbox({ src, onClose }: { src: string; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null)
  const close = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    // 记下"点开之前焦点在哪"（= 那张缩略图），关闭时还回去。
    const previous = document.activeElement as HTMLElement | null
    close.current?.focus()
    return () => previous?.focus()
  }, [])

  return createPortal(
    <div
      className={styles.backdrop}
      // 只有点在**背景本身**上才关：点在图上不关（用户是在看图，不是想关掉它 ✗）。
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-label="放大查看图片"
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.stopPropagation()
            onClose()
          }
          if (event.key === 'Tab') {
            // 面板里只有一个可聚焦元素（关闭按钮）：Tab 应当**自锁**，不能让焦点跑到背后的页面去。
            event.preventDefault()
            close.current?.focus()
          }
        }}
      >
        <img className={styles.image} src={src} alt="放大查看的图片" />
        <button
          ref={close}
          type="button"
          className={styles.close}
          aria-label="关闭放大视图"
          onClick={onClose}
        >
          ×
        </button>
      </div>
    </div>,
    document.body,
  )
}
