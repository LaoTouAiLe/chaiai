// React Query 层（T30）：会话、工作区与快照的服务端状态。
//
// 分工（计划 §6.3）：**服务端权威的东西全在这里**（快照、会话列表、工作区），本地 UI 状态在
// `stores/`。事件流只做两件事：把 `session_snapshot` 写进这里、把增量放进临时尾部（`stores/stream.ts`）。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef } from 'react'

import {
  ApiError,
  cancelTask,
  closeTaskReview,
  setSessionMode,
  endInterruptedRequest,
  createSession,
  addMcpServer,
  editMcpServer,
  deleteMcpServer,
  getCapabilities,
  getMcp,
  getSkills,
  getHealth,
  getTasks,
  getArtifacts,
  getMemoryPage,
  getUsagePage,
  getWorkspaceGitDiff,
  getWorkspaceGitStatus,
  listCronJobs,
  listCronRuns,
  listWorkspaceFiles,
  previewArtifact,
  searchWorkspaceFiles,
  getSessionSnapshot,
  listModelConnections,
  listSessions,
  listWorkspaces,
  newRequestId,
  pickWorkspaceDirectory,
  resolveApproval,
  selectSessionModel,
  setCapabilities,
  setMcpServer,
  setSkillEnabled,
  reloadMcp,
  addSkillRoot,
  removeSkillRoot,
  selectWorkspace,
  submitMessage,
  getSessionChanges,
  getSessionChangeDiff,
} from './client'
import type { components } from './schema.gen'
import type { SubmissionContext } from './client'
import type { ArtifactFilters, MemoryFilters, UsageFilters } from './client'

export function useMemoryPage(filters: MemoryFilters, enabled: boolean) {
  return useQuery({ queryKey: ['data-center', 'memory', filters], queryFn: () => getMemoryPage(filters), enabled, staleTime: 5000 })
}
export function useUsagePage(filters: UsageFilters, enabled: boolean) {
  return useQuery({ queryKey: ['data-center', 'usage', filters], queryFn: () => getUsagePage(filters), enabled, staleTime: 5000 })
}
/** 产物汇总（§15.11.1）：只读、服务端分页；窗口 `days` 参与 queryKey，换窗口要重新取。 */
export function useArtifacts(filters: ArtifactFilters, enabled: boolean) {
  return useQuery({ queryKey: ['data-center', 'artifacts', filters], queryFn: () => getArtifacts(filters), enabled, staleTime: 5000 })
}
/** 右栏文件面板（§21.6）：列一层目录。 */
export function useWorkspaceFiles(
  path: string,
  enabled: boolean,
  recursive = false,
  workspaceId?: string,
) {
  return useQuery({
    // `workspaceId` 必须进 key：不然切换工作区后拿到的是上一个工作区的缓存
    // （2026-10-23 实机反馈："切了工作区，文件列表还是第一个工作区的"）。
    queryKey: ['workspace', 'files', workspaceId ?? '', path, recursive],
    queryFn: () => listWorkspaceFiles(path, workspaceId, recursive),
    enabled,
    staleTime: 5000,
  })
}
/** 工作区文件名搜索（§21.6）：空串不发请求。 */
export function useSearchWorkspaceFiles(q: string) {
  return useQuery({
    queryKey: ['workspace', 'file-search', q],
    queryFn: () => searchWorkspaceFiles(q),
    enabled: q.trim().length >= 1,
    staleTime: 5000,
  })
}

export function useWorkspaceGitStatus(enabled: boolean) {
  return useQuery({
    queryKey: ['workspace', 'git-status'],
    queryFn: () => getWorkspaceGitStatus(),
    enabled,
    staleTime: 5000,
  })
}
export function useWorkspaceGitDiff(path: string | null) {
  return useQuery({
    queryKey: ['workspace', 'git-diff', path],
    queryFn: () => getWorkspaceGitDiff(path ?? ''),
    enabled: path !== null,
    staleTime: 5000,
  })
}
/**
 * 产物内容预览（§15.11.3）：`path=null` 时不发请求。
 *
 * `workspaceId` 必须与条目所属工作区一致——不传就会按"启动工作区"解析，
 * 在别的会话工作区里必然读不到文件。
 */
export function useArtifactsDetail(
  path: string | null,
  workspaceId?: string,
  scope: 'workspace' | 'attachments' = 'workspace',
) {
  return useQuery({
    queryKey: ['data-center', 'artifact-preview', scope, workspaceId ?? '', path],
    queryFn: () => previewArtifact(path ?? '', workspaceId, scope),
    enabled: path !== null,
    staleTime: 60_000,
  })
}
/** 定时任务列表（§21.5）。`scheduler_running=false` 时页面要说明"不会触发"。 */
export function useCronJobs(enabled = true) {
  return useQuery({ queryKey: ['cron', 'jobs'], queryFn: listCronJobs, enabled, staleTime: 5000 })
}
export function useCronRuns(jobId: string | null) {
  return useQuery({
    queryKey: ['cron', 'runs', jobId],
    queryFn: () => listCronRuns(jobId ?? '', 0, 20),
    enabled: jobId !== null,
    staleTime: 5000,
  })
}

export const queryKeys = {
  health: ['health'] as const,
  workspaces: ['workspaces'] as const,
  sessions: (workspaceId: string | null) => ['sessions', workspaceId] as const,
  snapshot: (sessionId: string) => ['snapshot', sessionId] as const,
  modelConnections: ['model-connections'] as const,
  capabilities: ['capabilities'] as const,
  mcp: ['mcp'] as const,
  skills: ['skills'] as const,
  tasks: (workspaceId: string | null) => ['tasks', workspaceId] as const,
  changes: (sessionId: string) => ['session-changes', sessionId] as const,
}

/**
 * 会话内的文件变更总览（2026-10-21）。
 *
 * 不使用轮询：一轮结束由 `useSessionStream` 的 `task_completed` 事件失效它；面板关着时
 * `enabled=false`，不产生任何请求。
 */
export function useSessionChanges(sessionId: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.changes(sessionId ?? ''),
    queryFn: () => getSessionChanges(sessionId ?? ''),
    enabled: sessionId !== null && enabled,
  })
}

/** 单个文件的整文件 diff；只在用户点开某个文件时才取。 */
export function useSessionChangeDiff(sessionId: string | null, path: string | null) {
  return useQuery({
    queryKey: [...queryKeys.changes(sessionId ?? ''), 'diff', path ?? ''],
    queryFn: () => getSessionChangeDiff(sessionId ?? '', path ?? ''),
    enabled: sessionId !== null && path !== null,
    staleTime: 5000,
  })
}

/**
 * 任务中心（T62；§17.6）：只读索引，按当前工作区取。
 *
 * **即时更新走事件，不靠快轮询**：`useSessionStream` 收到 §17.3 的任务事件就失效这份查询
 * （`job_queued` / `job_progress` / `job_waiting` / `job_completed` / `task_cancelled`）。
 *
 * 为什么还留一条 15 秒的兜底，它只管两种情况：① SSE 是**按会话订阅**的，页面只订阅当前打开的那个
 * 会话，因此**别的会话**（`jobs.background=true` 时的后台任务）状态变化没有事件能到这个页面；
 * ② 断线期间漏掉的事件不会重放（事件流没有持久游标，§15.6）。默认路径（`background=false`，任务就在
 * 本页提交的那一轮里跑）由事件即时更新，兜底不会成为"主要机制"。
 */
export function useTasks(workspaceId: string | null) {
  return useQuery({
    queryKey: queryKeys.tasks(workspaceId),
    queryFn: () => getTasks(workspaceId),
    refetchInterval: 15000,
  })
}

/**
 * 能力开关与能力状态（T56；§16.1）。
 *
 * 服务端每次现算，所以这里不设 `staleTime`：打开面板就会取一次最新的，
 * 用户改完开关再打开就能看到新结论（缓存一份过期答案比不显示更糟）。
 */
export function useCapabilities() {
  return useQuery({ queryKey: queryKeys.capabilities, queryFn: getCapabilities })
}

/**
 * 改能力开关（§16.1）：保存即生效、作用于下一轮、不用重启。
 *
 * 成功就用服务端返回的**完整视图**替换缓存——开关一变，每个能力的可用性与"下一步"都跟着变，
 * 自己在前端推算那份列表就是第二套真相（与 `usePermissions.setMode` 同一条做法）。
 */
export function useSetCapabilities() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (changes: Record<string, boolean>) => setCapabilities(changes),
    onSuccess: (view) => {
      queryClient.setQueryData(queryKeys.capabilities, view)
    },
  })
}

/**
 * MCP 面板（2026-10-20；契约 §6.13）：server 状态、工具与规模统计。
 *
 * 服务端每次现算（含重读 `mcp.json`），所以不设 `staleTime`：打开这一页就取一次最新的。
 */
export function useMcp() {
  return useQuery({ queryKey: queryKeys.mcp, queryFn: getMcp, refetchInterval: 2000 })
}

/** 改一台 server 的开关或名单：成功后用服务端返回的完整视图替换缓存。 */
export function useSetMcpServer() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ name, changes }: { name: string; changes: Record<string, unknown> }) =>
      setMcpServer(name, changes),
    onSuccess: view => {
      queryClient.setQueryData(queryKeys.mcp, view)
    },
  })
}

/** 新增一台 MCP Server：成功后用服务端返回的完整视图替换缓存（新 server 已进列表）。 */
export function useAddMcpServer() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: addMcpServer,
    onSuccess: view => {
      queryClient.setQueryData(queryKeys.mcp, view)
    },
  })
}

/** 重读 `mcp.json`；配置写坏时服务端返回带行号的 422，由调用方原样展示。 */
export function useReloadMcp() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: reloadMcp,
    onSuccess: view => {
      queryClient.setQueryData(queryKeys.mcp, view)
    },
  })
}

/**
 * 技能清单与启用状态（技能管理页，2026-10-16；§16.5）。
 *
 * 服务端每次现算（技能每轮重新发现），所以不设 `staleTime`：打开这一页就取一次最新的。
 */
export function useSkills() {
  return useQuery({ queryKey: queryKeys.skills, queryFn: getSkills })
}

/**
 * 启用/禁用一个技能（§16.5）：保存即生效、作用于下一轮。
 *
 * 成功就用服务端返回的**完整视图**替换缓存（与 `useSetCapabilities` 同一条做法）——总闸、诊断与
 * 每条技能的来源都跟着变，前端自己推算就是第二套真相。
 */
export function useSetSkill() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (change: { name: string; enabled: boolean }) =>
      setSkillEnabled(change.name, change.enabled),
    onSuccess: (view) => {
      queryClient.setQueryData(queryKeys.skills, view)
    },
  })
}

/** 添加一个技能发现根（2026-10-20）：保存即生效、作用于下一轮。 */
export function useAddSkillRoot() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (path: string) => addSkillRoot(path),
    onSuccess: (view) => {
      queryClient.setQueryData(queryKeys.skills, view)
    },
  })
}

/** 移除一个自定义发现根：不删文件，只是不再扫描。 */
export function useRemoveSkillRoot() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (path: string) => removeSkillRoot(path),
    onSuccess: (view) => {
      queryClient.setQueryData(queryKeys.skills, view)
    },
  })
}

export function useHealth() {
  return useQuery({ queryKey: queryKeys.health, queryFn: getHealth })
}export function useWorkspaces() {
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

export function useSetSessionMode() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ sessionId, mode, expectedMode }: { sessionId: string; mode: 'chat' | 'agent'; expectedMode: 'chat' | 'agent' }) =>
      setSessionMode(sessionId, mode, expectedMode),
    onSuccess: (session) => {
      queryClient.setQueryData<components['schemas']['SessionSnapshot']>(queryKeys.snapshot(session.session_id),
        (previous) => previous === undefined ? previous : { ...previous, mode: session.mode })
      queryClient.setQueriesData<components['schemas']['SessionSummaryView'][]>({ queryKey: ['sessions'] },
        (previous) => previous?.map((item) => item.session_id === session.session_id ? session : item))
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(session.session_id) })
    },
  })
}

/**
 * 取消当前会话在跑的任务（§17.4）：**幂等**——没有可取消的任务时是空操作，返回当时的结局。
 *
 * 成功后**重取快照**而不是本地改状态：这一轮的终态（`cancelled`）、已产出的部分回答与任务索引
 * 都由服务端说了算，页面自己推算一份就是第二套真相（与审批决定同一口径）。失败也重取——取消可能
 * 刚好被别处抢先处理，那只能由服务端说清楚。
 */
/** 人工核对结案（§17.1 唯一例外）：成功后刷任务索引，侧栏那行才会消失。 */
export function useCloseTaskReview() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (request: { jobId: string; outcome: 'completed' | 'cancelled' }) =>
      closeTaskReview(request.jobId, request.outcome),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
    },
  })
}

export function useCancelTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ sessionId, reason = '' }: { sessionId: string; reason?: string }) =>
      cancelTask(sessionId, reason),
    onSuccess: (_result, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
    },
    onError: (_error, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
    },
  })
}

export function useEndInterruptedRequest() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ sessionId, requestId, snapshotId }: { sessionId: string; requestId: string; snapshotId: string }) =>
      endInterruptedRequest(sessionId, requestId, snapshotId),
    onSettled: (_data, _error, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      // 侧栏那行的「执行中 / 等待审批」来自任务索引，不刷它会落后于实际状态。
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: ['conversation-map', request.sessionId] })
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
export function useSubmitMessage(sessionId: string | null, context: SubmissionContext = {}) {
  const queryClient = useQueryClient()
  const pending = useRef(new Map<string, { id: string; signature: string; context: SubmissionContext }>())

  const mutation = useMutation({
    mutationFn: async (request: { sessionId: string; text: string; context: SubmissionContext; attachments?: readonly string[] }) => {
      const existing = pending.current.get(request.sessionId)
      const text = request.text
      // 附件也进签名（2026-10-16）：与后端 `_digest` 同一条口径——"同一段文字、换一张图再发"
      // 是两个不同的请求，不能被当成上一次的重放（那样图就永远发不出去）。
      const attachments = [...(request.attachments ?? [])]
      const signature = JSON.stringify([text, request.context.parent_turn_id ?? null, request.context.expected_branch_id ?? null, request.context.workflow ?? null, attachments])
      const same = existing?.signature === signature
      const requestId = same ? existing.id : newRequestId()
      const boundContext = same ? existing.context : request.context
      pending.current.set(request.sessionId, { id: requestId, signature, context: boundContext })
      let result
      try {
        result = await submitMessage(request.sessionId, text, requestId, {
          ...boundContext,
          ...(attachments.length === 0 ? {} : { attachments }),
        })
      } catch (error) {
        // 明确拒绝且没有执行时，刷新后可换观测版本；网络中断仍保留原请求的上下文与幂等键。
        if (error instanceof ApiError && error.code === 'CONVERSATION_CONFLICT') pending.current.delete(request.sessionId)
        throw error
      }
      // 成功后换新键；未确认执行结果的失败保留原上下文，明确的路径拒绝已在上面清除。
      pending.current.delete(request.sessionId)
      return result
    },
    onSuccess: (result, request) => {
      // 提交返回的是**命令结果**（不是快照）：权威状态仍以快照为准，所以拉一次。
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
      // 侧栏那行的「执行中 / 等待审批」来自任务索引，不刷它会落后于实际状态。
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: ['conversation-map', request.sessionId] })
      return result
    },
    onError: (_error, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
    },
  })
  const belongsHere = mutation.variables?.sessionId === sessionId
  return {
    // 带的附件是**引用**（`AttachmentView.attachment_id`），不是文件本身；字节已经在上传接口落盘了。
    mutate: (
      // `context` 可**按次覆盖**：行内编辑（2026-10-21）要临时换 `parent_turn_id`，
      // 而它记在 hook 参数里、渲染期就定了。合并而不是整体替换，是因为其余几项
      // （branch / snapshot / workflow）仍该沿用当前上下文。
      request: { text: string; attachments?: readonly string[]; context?: SubmissionContext },
      options?: Parameters<typeof mutation.mutate>[1],
    ) => {
      if (sessionId === null) throw new Error('还没有打开会话')
      mutation.mutate({
        sessionId,
        text: request.text,
        context: { ...context, ...(request.context ?? {}) },
        attachments: request.attachments,
      }, options)
    },
    isPending: belongsHere && mutation.isPending,
    isError: belongsHere && mutation.isError,
    error: belongsHere ? mutation.error : null,
    reset: mutation.reset,
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
      // 侧栏那行「需人工核对」来自**任务索引**，不是快照（§19.1）。
      // 只刷快照的话，审批点了批准，列表里那行还要等它的 15 秒轮询才变
      // ——用户看到的就是「点了之后没有任何联动」（2026-10-23 实机反馈）。
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
      void queryClient.invalidateQueries({ queryKey: ['sessions'] })
    },
    // 失败也要重取：过期与被别处抢先处理都只能由服务端说清楚，页面不替它下结论。
    onError: (_error, request) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.snapshot(request.sessionId) })
      void queryClient.invalidateQueries({ queryKey: ['tasks'] })
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
    reset: mutation.reset,
  }
}

/** 透传用的小工具：把任意异常收敛成 `ApiError | Error`，页面只按 `code` 分支。 */
export function asApiError(error: unknown): ApiError | Error {
  return error instanceof Error ? error : new Error(String(error))
}

export type Snapshot = components['schemas']['SessionSnapshot']
export type SessionSummary = components['schemas']['SessionSummaryView']
export type Workspace = components['schemas']['WorkspaceRef']

export function useEditMcpServer() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ name, changes }: { name: string; changes: Record<string, unknown> }) =>
      editMcpServer(name, changes),
    onSuccess: view => queryClient.setQueryData(queryKeys.mcp, view),
  })
}

export function useDeleteMcpServer() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteMcpServer,
    onSuccess: view => queryClient.setQueryData(queryKeys.mcp, view),
  })
}
