// React Query 层（T30）：会话、工作区与快照的服务端状态。
//
// 分工（计划 §6.3）：**服务端权威的东西全在这里**（快照、会话列表、工作区），本地 UI 状态在
// `stores/`。事件流只做两件事：把 `session_snapshot` 写进这里、把增量放进临时尾部（`stores/stream.ts`）。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef } from 'react'

import {
  ApiError,
  createSession,
  getHealth,
  getSessionSnapshot,
  listModelConnections,
  listSessions,
  listWorkspaces,
  newRequestId,
  pickWorkspaceDirectory,
  resolveApproval,
  selectSessionModel,
  selectWorkspace,
  submitMessage,
} from './client'
import type { components } from './schema.gen'

export const queryKeys = {
  health: ['health'] as const,
  workspaces: ['workspaces'] as const,
  sessions: (workspaceId: string | null) => ['sessions', workspaceId] as const,
  snapshot: (sessionId: string) => ['snapshot', sessionId] as const,
  modelConnections: ['model-connections'] as const,
}

export function useHealth() {
  return useQuery({ queryKey: queryKeys.health, queryFn: getHealth })
}

export function useWorkspaces() {
  return useQuery({ queryKey: queryKeys.workspaces, queryFn: listWorkspaces })
}

export function useSessions(workspaceId: string | null) {
  return useQuery({
    queryKey: queryKeys.sessions(workspaceId),
    queryFn: () => listSessions(workspaceId ?? undefined),
  })
}

export function useArchivedSessions(enabled: boolean) {
  return useQuery({
    queryKey: ['sessions', 'archived'],
    queryFn: () => listSessions(undefined, true),
    enabled,
  })
}

/** 模型连接列表：页面用它把"会话选中的连接 id"翻成可读的模型标签。 */
export function useModelConnections() {
  return useQuery({
    queryKey: queryKeys.modelConnections,
    queryFn: listModelConnections,
    // 连接随时可能在设置里改（新增/编辑/停用），这里不做长缓存。
    staleTime: 0,
  })
}

/** 会话选择只影响下一轮；写入要带当前观测与目标连接版本，冲突由用户刷新处理。 */
export function useSelectSessionModel(sessionId: string | null) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (choice: {
      connectionId: string | null
      expectation: components['schemas']['SelectionExpectation']
      expectedVersion: number | null
    }) => {
      if (sessionId === null) throw new Error('还没有打开会话')
      return selectSessionModel(sessionId, choice.connectionId, choice.expectation, choice.expectedVersion)
    },
    onSuccess: (snapshot) => queryClient.setQueryData(queryKeys.snapshot(snapshot.session_id), snapshot),
  })
}

export function useSnapshot(sessionId: string | null) {
  return useQuery({
    queryKey: queryKeys.snapshot(sessionId ?? ''),
    queryFn: () => getSessionSnapshot(sessionId ?? ''),
    enabled: sessionId !== null,
  })
}

/**
 * 弹出本机原生目录选择器。
 *
 * **刻意不改任何缓存**：这个接口不改变状态（取消也是零副作用），拿到路径之后要不要登记由调用方
 * 决定——登记走 `useSelectWorkspace`，缓存失效规则只留在那一处。
 */
export function usePickWorkspaceDirectory() {
  return useMutation({ mutationFn: pickWorkspaceDirectory })
}

/** 选工作区（或新建）：返回切过去的会话，调用方据此打开它。 */
export function useSelectWorkspace() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: selectWorkspace,
    onSuccess: () => {
      // 工作区切换会连带改变会话归属，两份缓存都要失效。
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
    },
  })
}

export function useCreateSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ workspaceId, mode }: { workspaceId: string | null; mode: 'chat' | 'agent' }) =>
      createSession(workspaceId === null ? { mode } : { workspace_id: workspaceId, mode }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.workspaces })
    },
  })
}

/**
 * 提交一条消息。
 *
 * **幂等键的生命周期**是这里唯一容易做错的地方（§6.1）：
 * - 同一次提交的**重试**必须复用同一个 `client_request_id`——换一个就等于告诉服务端"这是一条新消息"，
 *   于是真重复执行；
 * - 用户**改了文字**再提交就是**另一条消息**，必须换键——否则服务端按摘要判重放，
 *   第二条消息会被静默丢弃（或被判成 `IDEMPOTENCY_CONFLICT`）。
 * 所以键与它当时对应的文本绑在一起记。
 */
export function useSubmitMessage(sessionId: string | null) {
  const queryClient = useQueryClient()
  const pending = useRef(new Map<string, { id: string; text: string }>())

  const mutation = useMutation({
    mutationFn: async (request: { sessionId: string; text: string }) => {
      const existing = pending.current.get(request.sessionId)
      const text = request.text
      const requestId = existing !== undefined && existing.text === text ? existing.id : newRequestId()
      pending.current.set(request.sessionId, { id: requestId, text })
      const result = await submitMessage(request.sessionId, text, requestId)
      // 成功之后才换新的：失败（含忙碌、冲突、网络中断）都保留，重试仍指向同一次提交。
      pending.current.delete(request.sessionId)
      return result
    },
    onSuccess: (result, request) => {
      // 提交返回的是**命令结果**（不是快照）：权威状态仍以快照为准，所以拉一次。
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      return result
    },
  })
  const belongsHere = mutation.variables?.sessionId === sessionId
  return {
    mutate: (text: string, options?: Parameters<typeof mutation.mutate>[1]) => {
      if (sessionId === null) throw new Error('还没有打开会话')
      mutation.mutate({ sessionId, text }, options)
    },
    isPending: belongsHere && mutation.isPending,
    isError: belongsHere && mutation.isError,
    error: belongsHere ? mutation.error : null,
  }
}

/**
 * 提交一次审批决定（批准 / 拒绝）。
 *
 * 与 `useSubmitMessage` 同一套幂等键纪律（§6.1）：同一次决定的**重试**必须复用同一个
 * `client_request_id`，换了就等于告诉服务端"这是第二个决定"。键按 `审批 id + 决定` 记——
 * 同一次审批先点拒绝、再点批准是**两个**决定，不能共用键；而"点批准失败后再点批准"是同一次决定。
 *
 * **后端结果是权威**：成功与失败都重取快照，页面不自己推断审批变成了什么状态（§15.8）。
 */
export function useResolveApproval(sessionId: string | null) {
  const queryClient = useQueryClient()
  const pending = useRef(new Map<string, string>())

  const mutation = useMutation({
    mutationFn: async (request: { sessionId: string; approvalId: string; decision: 'approve' | 'reject'; confirmRecursive: boolean }) => {
      const scope = `${request.approvalId}:${request.decision}:${String(request.confirmRecursive)}`
      const requestId = pending.current.get(scope) ?? newRequestId()
      pending.current.set(scope, requestId)
      const result = await resolveApproval(request.sessionId, request.approvalId, {
        decision: request.decision,
        client_request_id: requestId,
        confirm_recursive: request.confirmRecursive,
      })
      // 成功之后才换新的：失败（过期、忙碌、网络中断）都保留，重试仍指向同一个决定。
      pending.current.delete(scope)
      return result
    },
    onSuccess: (_result, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
    },
    // 失败也要重取：过期与被别处抢先处理都只能由服务端说清楚，页面不替它下结论。
    onError: (_error, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
    },
  })

  const belongsHere = mutation.variables?.sessionId === sessionId
  return {
    resolve: (approvalId: string, decision: 'approve' | 'reject', confirmRecursive = false) => {
      if (sessionId === null) throw new Error('还没有打开会话')
      mutation.mutate({ sessionId, approvalId, decision, confirmRecursive })
    },
    isPending: belongsHere && mutation.isPending,
    error: belongsHere ? mutation.error : null,
  }
}

/** 透传用的小工具：把任意异常收敛成 `ApiError | Error`，页面只按 `code` 分支。 */
export function asApiError(error: unknown): ApiError | Error {
  return error instanceof Error ? error : new Error(String(error))
}

export type Snapshot = components['schemas']['SessionSnapshot']
export type SessionSummary = components['schemas']['SessionSummaryView']
export type Workspace = components['schemas']['WorkspaceRef']
