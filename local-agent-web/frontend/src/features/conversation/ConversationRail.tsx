// 会话左侧的"问题进度条"（2026-10-19）：一条提问一根刻度，点一下跳到那条提问。
//
// 口径：
//   1. 只列**用户提问**（每轮的第一条消息是用户消息才算）。助手回答、工具行不进刻度条——
//      要跳的是"我问过什么"，不是执行过程；
//   2. 悬停显示问题原文（长文截断），当前视口所在的提问用最深的颜色标出，已划过的变深；
//   3. 只有提问 ≥ 2 条且内容超出一屏时才出现：一篇没有滚动的会话不需要导航。
//
// 位置贴在会话区最左边，不随滚动移动；未选中的刻度同长同色，只有当前提问加长变深，
// 切换时靠 CSS 过渡做出滑动感。窄窗下统一压窄（compact），避免盖住正文。

import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

import styles from './ConversationRail.module.css'

export interface RailQuestion {
  /** 轮首消息 id，与消息列上的 `data-turn-id` 一致。 */
  id: string
  /** 用户提问原文（已压成一行）；只发图时为"图片消息"。 */
  label: string
}

interface ConversationRailProps {
  scrollRef: RefObject<HTMLDivElement | null>
  questions: readonly RailQuestion[]
  /** 跳转时通知容器：用户离开了底部，后续流式增量不要再把他拽回去。 */
  onJump?: () => void
}

interface ScrollState {
  overflow: boolean
  active: number
  /** 窄窗：消息列紧贴左边，刻度只能压窄、贴到最左，免得盖住正文。 */
  compact: boolean
}

/** 提问在滚动内容里的纵坐标（不受滚动位置影响）。 */
function topOf(pane: HTMLElement, element: HTMLElement): number {
  return element.getBoundingClientRect().top - pane.getBoundingClientRect().top + pane.scrollTop
}

export function ConversationRail({ scrollRef, questions, onJump }: ConversationRailProps) {
  const [state, setState] = useState<ScrollState>({ overflow: false, active: 0, compact: false })
  const [hovered, setHovered] = useState<number | null>(null)
  // 流式回答每来一段都会重建 `questions`；测量函数若依赖它，观察器会跟着重建几十次。
  // 这里用 ref 取最新的一份，订阅只在挂载/切换会话时建立。
  const questionsRef = useRef(questions)
  questionsRef.current = questions

  const measure = useCallback(() => {
    const pane = scrollRef.current
    if (pane === null) return
    const list = questionsRef.current
    const max = pane.scrollHeight - pane.clientHeight
    const atBottom = max - pane.scrollTop <= 24
    const elements = Array.from(pane.querySelectorAll<HTMLElement>('[data-turn-id]'))
    const tops = list.map((question) => {
      const element = elements.find((item) => item.dataset.turnId === question.id)
      return element === undefined ? null : topOf(pane, element)
    })
    let active = 0
    tops.forEach((top, index) => {
      if (top !== null && top <= pane.scrollTop + 40) active = index
    })
    if (atBottom) active = Math.max(0, list.length - 1)
    const overflow = max > 24
    const compact = pane.clientWidth < 800
    setState((current) => (
      current.overflow === overflow && current.active === active && current.compact === compact
        ? current
        : { overflow, active, compact }
    ))
  }, [scrollRef])

  useEffect(() => {
    const pane = scrollRef.current
    if (pane === null) return undefined
    let frame = 0
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
    }
    measure()
    // 流式回答变长、切换会话都会改变高度；只靠 scroll 事件判断不到。
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
  }, [measure, scrollRef])

  const jump = (question: RailQuestion) => {
    const pane = scrollRef.current
    if (pane === null) return
    const element = Array.from(pane.querySelectorAll<HTMLElement>('[data-turn-id]'))
      .find((item) => item.dataset.turnId === question.id)
    if (element === undefined) return
    onJump?.()
    pane.scrollTo({ top: Math.max(0, topOf(pane, element) - 12), behavior: 'smooth' })
  }

  if (questions.length < 2 || !state.overflow) return null

  return (
    <div
      className={`${styles.rail} ${state.compact ? styles.railCompact : ''}`}
      role="group"
      aria-label="问题跳转"
    >
      {questions.map((question, index) => (
        <div key={question.id} className={styles.slot}>
          <button
            type="button"
            className={`${styles.tick} ${index === state.active ? styles.tickActive : ''}`}
            aria-label={`跳到：${question.label}`}
            aria-current={index === state.active ? 'true' : undefined}
            onClick={() => jump(question)}
            onMouseEnter={() => setHovered(index)}
            onMouseLeave={() => setHovered(null)}
            onFocus={() => setHovered(index)}
            onBlur={() => setHovered(null)}
          >
            <span aria-hidden="true" />
          </button>
          {hovered === index ? (
            <span className={styles.preview} aria-hidden="true">
              <strong>{question.label}</strong>
              <small>点击跳到这条提问</small>
            </span>
          ) : null}
        </div>
      ))}
    </div>
  )
}
