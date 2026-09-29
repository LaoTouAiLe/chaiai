// 本地 UI 状态（T30；计划 §6.3："React Query 管会话与快照；Zustand 只保存选中项、展开状态、
// 草稿等本地 UI"）。
//
// 边界很清楚，别把服务端状态搬进来：
// - **会话内容、快照、连接列表**都是服务端权威 → React Query（`src/api/queries.ts`）；
// - 这里只放"刷新就没了也无所谓"的东西：当前选中哪个工作区/会话、每个会话各自的草稿。
//   草稿要留的原因是 T30 的裁定：任务执行中**允许继续写**，切走再回来也不该丢。
//
// 刻意**不持久化**：刷新后"打开哪个会话"由服务端决定（`SessionSummaryView.is_active` 是权威），
// 本地再存一份就会和它对不上。

import { create } from 'zustand'

export interface UiState {
  /** 当前工作区；`null` = 还没选（服务端有默认工作区时会由它回填）。 */
  workspaceId: string | null
  /** 当前打开的会话；`null` = 还没有会话。 */
  activeSessionId: string | null
  /** 会话 id → 草稿文本。空串表示用户把草稿清空了（保留键值，避免"回到默认"的歧义）。 */
  drafts: Record<string, string>

  setWorkspace: (workspaceId: string | null) => void
  openSession: (sessionId: string | null) => void
  setDraft: (sessionId: string, text: string) => void
}

export const useUiStore = create<UiState>((set) => ({
  workspaceId: null,
  activeSessionId: null,
  drafts: {},

  setWorkspace: (workspaceId) => set({ workspaceId }),
  openSession: (activeSessionId) => set({ activeSessionId }),
  setDraft: (sessionId, text) => set((state) => ({ drafts: { ...state.drafts, [sessionId]: text } })),
}))
