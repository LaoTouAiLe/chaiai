// 流式尾部**存放处**（T30）。
//
// 这里刻意只做"存/取/清"：判定逻辑（去重、缺块、与快照对账）全在
// `features/conversation/streamTail.ts` 的纯函数里，事件流钩子负责调用它们。
// 容器只存结果，不藏规则——规则散了就没法单独测。

import { create } from 'zustand'

import type { StreamTail } from '../features/conversation/streamTail'

export interface StreamState {
  /** 会话 id → 该会话正在生成的那条消息的临时尾部。 */
  tails: Record<string, StreamTail | null>
  setTail: (sessionId: string, tail: StreamTail | null) => void
}

export const useStreamStore = create<StreamState>((set) => ({
  tails: {},
  setTail: (sessionId, tail) => set((state) => ({ tails: { ...state.tails, [sessionId]: tail } })),
}))

/** 读某个会话的尾部（组件用 `useStreamTail`；事件流钩子用这个直接从 store 取当前值）。 */
export function tailOf(tails: Record<string, StreamTail | null>, sessionId: string): StreamTail | null {
  return tails[sessionId] ?? null
}

/** 组件侧的订阅：只跟这一个会话的尾部，别的会话变化不触发重渲染。 */
export function useStreamTail(sessionId: string | null): StreamTail | null {
  return useStreamStore((state) => (sessionId === null ? null : (state.tails[sessionId] ?? null)))
}
