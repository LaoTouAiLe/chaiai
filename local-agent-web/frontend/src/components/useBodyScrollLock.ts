// 锁住背后页面的滚动（2026-10-26）。
//
// 为什么需要：模态弹层的遮罩是 `position: fixed` 且**本身不可滚动**，真正滚动的是里面的列表。
// 列表滚到边界后，浏览器会沿 DOM 链把滚轮继续传给父级（scroll chaining），一路传到 `body`，
// 于是背后的会话列表与输入框跟着动。`overscroll-behavior: contain` 只能挡住"可滚动元素"的
// 边界，挡不住鼠标停在遮罩空白处的滚轮——那一处只能靠锁 `body`。
//
// 为什么用**计数**而不是布尔：任务中心、能力中心、设置理论上可能先后或同时存在（例如能力中心
// 从设置里打开）。布尔实现下，先关闭的那个会把 `overflow` 还原，而另一个还开着——背后又开始滚。

import { useEffect } from 'react'

/** 当前有多少个弹层持有锁。模块级：弹层之间没有共享父组件可以放这个状态。 */
let holders = 0
/** 第一个持有者进来之前的 `body.style.overflow`（通常是空串）。 */
let previousOverflow = ''

/**
 * 在 `active` 为真期间锁住 `document.body` 的滚动。
 *
 * Args:
 *     active: 是否生效。省略即"挂载期间一直生效"——弹层组件通常由父组件条件渲染，
 *         挂载本身就表示打开。内嵌模式（如能力面板嵌在整页里）要传 `false`。
 */
export function useBodyScrollLock(active = true): void {
  useEffect(() => {
    if (!active) return
    if (holders === 0) {
      previousOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    holders += 1
    return () => {
      holders -= 1
      // 只在最后一个持有者离开时还原：中途还原会让仍然打开的弹层背后重新开始滚动。
      if (holders === 0) document.body.style.overflow = previousOverflow
    }
  }, [active])
}
