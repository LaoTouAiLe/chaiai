// 待发队列（2026-10-28）：本轮还在跑时，用户仍然可以写、可以回车；消息先排队，本轮一结束
// 自动发出下一条。
//
// 边界与 `ui.ts` 一致：这是**界面状态**，不落服务端、不持久化。服务端也不排队——§15.2 的
// `SESSION_BUSY` 就是"本轮进行中再次提交、不进入队列"的意思，所以队列只能存在于页面里，
// 由 `LiveApp` 在会话重新可发送时逐条提交。
//
// 两条刻意的限制（用户已确认）：
// - **刷新即丢**：刷新后自动代发用户很久以前写的一句话，风险大于便利；
// - **点过"停止"就暂停**：用户按停止是为了打断，不该立刻又自动发下一条；自动发送失败同样暂停。

import { create } from 'zustand'

/** 队列上限：到了就不再接受，由页面提示，而不是无限堆积。 */
export const OUTBOX_LIMIT = 20

export interface QueuedMessage {
  id: string
  /** 已经拼好引用的最终文本；编辑只更新这条尚未发送的消息。 */
  text: string
}

export interface OutboxState {
  bySession: Record<string, QueuedMessage[]>
  /** 会话 id → 是否暂停（用户点过停止，或自动发送失败）。 */
  paused: Record<string, boolean>

  /** 入队；返回 false 表示没接受（空文本或已达上限），调用方据此保留草稿。 */
  enqueue: (sessionId: string, text: string) => boolean
  remove: (sessionId: string, id: string) => void
  edit: (sessionId: string, id: string, text: string) => boolean
  move: (sessionId: string, id: string, direction: 'up' | 'down') => void
  /** 取队首并从队列里摘掉（发送失败时由调用方 `restore` 放回队首）。 */
  shift: (sessionId: string) => QueuedMessage | null
  /** 把没发出去的一条放回**队首**（自动发送失败时用），不检查上限。 */
  restore: (sessionId: string, message: QueuedMessage) => void
  pause: (sessionId: string) => void
  resume: (sessionId: string) => void
  reset: (sessionId?: string) => void
}

let counter = 0

function nextId(): string {
  counter += 1
  return `q-${Date.now().toString(36)}-${counter.toString(36)}`
}

function without(
  state: OutboxState,
  sessionId: string,
  pick: (items: QueuedMessage[]) => QueuedMessage[],
): Record<string, QueuedMessage[]> {
  const items = state.bySession[sessionId] ?? []
  return { ...state.bySession, [sessionId]: pick(items) }
}

export const useOutbox = create<OutboxState>((set, get) => ({
  bySession: {},
  paused: {},

  enqueue: (sessionId, text) => {
    const trimmed = text.trim()
    if (trimmed === '') return false
    const items = get().bySession[sessionId] ?? []
    if (items.length >= OUTBOX_LIMIT) return false
    set((state) => ({
      bySession: { ...state.bySession, [sessionId]: [...items, { id: nextId(), text: trimmed }] },
    }))
    return true
  },

  remove: (sessionId, id) => {
    set((state) => ({ bySession: without(state, sessionId, (items) => items.filter((item) => item.id !== id)) }))
  },
  edit: (sessionId, id, text) => {
    if (!text.trim() || !get().bySession[sessionId]?.some(item => item.id === id)) return false
    set(state => ({ bySession: without(state, sessionId, items => items.map(item =>
      item.id === id ? { ...item, text: text.trim() } : item)) }))
    return true
  },

  move: (sessionId, id, direction) => {
    set((state) => ({
      bySession: without(state, sessionId, (items) => {
        const index = items.findIndex((item) => item.id === id)
        if (index < 0) return items
        const target = direction === 'up' ? index - 1 : index + 1
        if (target < 0 || target >= items.length) return items
        const next = [...items]
        const [moved] = next.splice(index, 1)
        if (moved === undefined) return items
        next.splice(target, 0, moved)
        return next
      }),
    }))
  },

  shift: (sessionId) => {
    const items = get().bySession[sessionId] ?? []
    const first = items[0]
    if (first === undefined) return null
    set((state) => ({ bySession: without(state, sessionId, (list) => list.filter((item) => item.id !== first.id)) }))
    return first
  },

  restore: (sessionId, message) => {
    set((state) => ({
      bySession: without(state, sessionId, (items) =>
        items.some((item) => item.id === message.id) ? items : [message, ...items],
      ),
    }))
  },

  pause: (sessionId) => {
    set((state) => ({ paused: { ...state.paused, [sessionId]: true } }))
  },

  resume: (sessionId) => {
    set((state) => ({ paused: { ...state.paused, [sessionId]: false } }))
  },

  reset: (sessionId) => {
    if (sessionId === undefined) {
      set({ bySession: {}, paused: {} })
      return
    }
    set((state) => ({
      bySession: without(state, sessionId, () => []),
      paused: { ...state.paused, [sessionId]: false },
    }))
  },
}))
