// 传输层客户端（T29A）：只认**生成的**契约类型，不手写接口形状。
//
// 分工（ARCHITECTURE §15.1）：本模块管"发请求、解析信封、把失败变成可执行的东西"；
// 页面用的展示视图模型在 `features/*/adapters.ts` 里映射，两者不混。
//
// 两条硬规则：
// 1. **只走相对路径 `/api`**：生产同源（由 Python 服务托管），开发由 Vite 代理到 127.0.0.1:8000；
//    客户端从不拼绝对地址，也就不会把请求发到用户没配过的机器上。
// 2. **错误分两种形状**：业务错误是 `{"error": ErrorView}`，传输层拒绝（403/413/422）是
//    `{"detail": ...}`。两者都要能被调用方区分，且都**不得**把响应原文当作展示文本直接抛给用户。

import type { components, paths } from './schema.gen'

export type ErrorView = components['schemas']['ErrorView']
export type ModelConnectionDto = components['schemas']['ModelConnectionView']
export type HealthDto = components['schemas']['HealthView']
export type PermissionsDto = components['schemas']['PermissionsView']
export type PreferencesDto = components['schemas']['PreferencesView']
export type PreferenceValues = components['schemas']['PreferenceValues']
export type GeneralPreferences = components['schemas']['GeneralPreferences']
export type ExternalPathsModeDto = PermissionsDto['mode']
export type ExternalPathAccessDto = components['schemas']['ExternalPathGrantView']['access']

/** 生成契约里的 200 响应体。 */
type Ok<T> = T extends { responses: { 200: { content: { 'application/json': infer R } } } } ? R : never

type ListConnectionsOk = Ok<paths['/api/model-connections']['get']>
type CreateConnectionBody = paths['/api/model-connections']['post']['requestBody'] extends {
  content: { 'application/json': infer B }
}
  ? B
  : never
type UpdateConnectionBody = paths['/api/model-connections/{connection_id}']['patch'] extends {
  requestBody: { content: { 'application/json': infer B } }
}
  ? B
  : never

export type ModelConnectionInput = CreateConnectionBody
export type ModelConnectionPatch = UpdateConnectionBody
export type ModelConnectionList = ListConnectionsOk

/**
 * 一次调用的失败。
 *
 * - `code` 为 `null` 表示**传输层**拒绝（403/413/422/500）：那是边界或框架给的，不是业务结论，
 *   调用方不该按业务码去分支，只应说明"请求没能到达业务逻辑"。
 * - `code` 有值时要按 §15.5 用 `code` / `retryable` / `hint` 决定措辞与是否提供重试，而不是解析 message。
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly hint: string | null
  readonly retryable: boolean
  readonly detail: string | null

  constructor(args: {
    status: number
    code: string | null
    message: string
    hint?: string | null
    retryable?: boolean
    detail?: string | null
  }) {
    super(args.message)
    this.name = 'ApiError'
    this.status = args.status
    this.code = args.code
    this.hint = args.hint ?? null
    this.retryable = args.retryable ?? false
    this.detail = args.detail ?? null
  }
}

/** 幂等键：`crypto.randomUUID` 在安全上下文之外可能缺席，退回到随机串。 */
export function newRequestId(): string {
  const cryptoRef = globalThis.crypto
  if (cryptoRef && typeof cryptoRef.randomUUID === 'function') return cryptoRef.randomUUID()
  return `req-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function parseJson(text: string): unknown {
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    // 不是 JSON（例如代理返回的 HTML）：按"没有可解析的错误体"处理，不抛。
    return null
  }
}

/** 把两种错误体收敛成一个 `ApiError`（业务信封优先）。SSE 建连失败也走它，口径才一致。 */
export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => '')
  const parsed = parseJson(text)

  if (isRecord(parsed) && isRecord(parsed.error)) {
    const error = parsed.error as unknown as ErrorView
    return new ApiError({
      status: response.status,
      code: typeof error.code === 'string' ? error.code : null,
      message: typeof error.message === 'string' ? error.message : '请求失败',
      hint: typeof error.hint === 'string' ? error.hint : null,
      retryable: error.retryable === true,
    })
  }

  const detail = isRecord(parsed) && typeof parsed.detail === 'string' ? parsed.detail : null
  return new ApiError({
    status: response.status,
    code: null,
    message: detail ?? `请求失败（HTTP ${response.status}）`,
    detail,
  })
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      // 只在带正文时声明 JSON：GET 带 content-type 是没必要的噪声。
      ...(init?.body === undefined ? {} : { 'content-type': 'application/json' }),
      ...(init?.headers ?? {}),
    },
  })
  if (!response.ok) throw await apiErrorFromResponse(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

// ---- 健康检查与会话（T30 会用得更多，这里先给设置页需要的两个）----------------

export function getHealth(): Promise<HealthDto> {
  return request<HealthDto>('/api/health')
}

/** 会话快照；`PUT /sessions/{id}/model` 的响应是它，页面据此显示"下一轮用哪条"。 */
export function getSessionSnapshot(sessionId: string): Promise<components['schemas']['SessionSnapshot']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}`)
}

// ---- 工作区与会话（T30 的纵向切片：选择工作区 → 会话 → 提交 → 恢复）---------

type Schemas = components['schemas']

export type BrowserDto = Schemas['BrowserView']
export type BrowserInput = Partial<Schemas['BrowserCommand']> & Pick<Schemas['BrowserCommand'], 'action'>
export type BrowserMessage = Schemas['BrowserFrame'] | Schemas['BrowserStateMessage']

export function createBrowser(requestId: string, workspaceId: string | null): Promise<BrowserDto> {
  return request('/api/browsers', { method: 'POST', body: JSON.stringify({ request_id: requestId, workspace_id: workspaceId }) })
}

export async function closeBrowser(requestId: string): Promise<void> {
  const response = await fetch(`/api/browsers/requests/${encodeURIComponent(requestId)}`, { method: 'DELETE', keepalive: true })
  if (!response.ok) throw await apiErrorFromResponse(response)
}

export type TerminalDto = Schemas['TerminalView']

export function listTerminals(workspaceId: string): Promise<Schemas['TerminalList']> {
  return request(`/api/terminals?workspace_id=${encodeURIComponent(workspaceId)}`)
}

export function requestTerminal(workspaceId: string): Promise<TerminalDto> {
  return request('/api/terminals/requests', { method: 'POST', body: JSON.stringify({ workspace_id: workspaceId }) })
}

export function decideTerminal(approvalId: string, decision: 'approve' | 'reject'): Promise<TerminalDto> {
  return request(`/api/terminals/requests/${encodeURIComponent(approvalId)}/decision`, {
    method: 'POST', body: JSON.stringify({ decision }),
  })
}

export function stopTerminal(terminalId: string): Promise<TerminalDto> {
  return request(`/api/terminals/${encodeURIComponent(terminalId)}`, { method: 'DELETE' })
}

/**
 * 会话内的文件变更总览（2026-10-21）。
 *
 * 数据来自服务端的**变更账本**（写盘时记账 + 首次改动前留基线），不是从工具消息里的事件拼的：
 * 一个文件被 edit 四次时，事件里只有四段互不相干的片段，拼不出"这个文件整体变成什么样"。
 */
export function getSessionChanges(sessionId: string): Promise<Schemas['SessionChangesView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/changes`)
}

/**
 * 把正在前台等待的命令转入后台（2026-10-21）。
 *
 * 返回 `false` 表示这条命令已经不在等（跑完了或 id 对不上）——页面据此如实说明，
 * 不能假装它已经在后台。
 */
export function detachCommand(
  sessionId: string,
  commandId: string,
): Promise<{ detached: boolean }> {
  return request(
    `/api/sessions/${encodeURIComponent(sessionId)}/commands/${encodeURIComponent(commandId)}/detach`,
    { method: 'POST' },
  )
}

/** 某个文件的**整文件** diff：首次改动前原文 vs 当前磁盘内容。 */
export function getSessionChangeDiff(
  sessionId: string,
  path: string,
): Promise<Schemas['FileChangeDiffView']> {
  return request(
    `/api/sessions/${encodeURIComponent(sessionId)}/changes/diff?path=${encodeURIComponent(path)}`,
  )
}

export function listWorkspaces(): Promise<Schemas['WorkspaceRef'][]> {
  return request('/api/workspaces')
}

/** 选择或新增工作区；返回**切过去的那个会话**（工作区与会话一对一绑定，§10.6）。 */
export function selectWorkspace(payload: {
  workspace_id?: string
  path?: string
}): Promise<Schemas['SessionSummaryView']> {
  return request('/api/workspaces/select', { method: 'POST', body: JSON.stringify(payload) })
}

export type PickDirectoryDto = Ok<paths['/api/workspaces/pick']['post']>

/**
 * 弹出**本机**原生目录选择器（宿主能力，不是业务接口）。
 *
 * 取消是正常操作：返回 `{ path: null }`，既不抛错也不改变任何状态——选中之后由调用方接着调
 * `selectWorkspace({ path })` 完成登记，校验与幂等全在那条路上。
 */
export function pickWorkspaceDirectory(): Promise<PickDirectoryDto> {
  return request('/api/workspaces/pick', { method: 'POST' })
}

/** 会话列表；按工作区过滤时传 `workspace_id`。 */
export function listSessions(workspaceId?: string, archived = false): Promise<Schemas['SessionSummaryView'][]> {
  const query = new URLSearchParams()
  if (workspaceId !== undefined) query.set('workspace_id', workspaceId)
  if (archived) query.set('archived', 'true')
  return request(`/api/sessions${query.size === 0 ? '' : `?${query}`}`)
}

/** 会话搜索（§15.11.2）：匹配标题与用户提问，跨全部会话。 */
export function searchSessions(q: string, limit = 20): Promise<Schemas['SessionSummaryView'][]> {
  const query = new URLSearchParams({ q, limit: String(limit) })
  return request(`/api/sessions?${query}`)
}

export function selectSession(id: string): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(id)}/select`, { method: 'POST' })
}

export function setSessionMode(id: string, mode: 'chat' | 'agent', expectedMode: 'chat' | 'agent'): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(id)}/mode`, {
    method: 'PATCH', body: JSON.stringify({ mode, expected_mode: expectedMode }),
  })
}

export function endInterruptedRequest(id: string, requestId: string, snapshotId: string): Promise<Schemas['SessionSnapshot']> {
  return request(`/api/sessions/${encodeURIComponent(id)}/recovery`, {
    method: 'POST', body: JSON.stringify({ request_id: requestId, expected_snapshot_id: snapshotId, confirm_reviewed: true }),
  })
}

export function renameSession(id: string, title: string): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ title }) })
}

export function archiveSession(id: string): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(id)}/archive`, { method: 'POST' })
}

export function restoreSession(id: string): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(id)}/restore`, { method: 'POST' })
}

export function renameWorkspace(id: string, title: string): Promise<Schemas['WorkspaceRef']> {
  return request(`/api/workspaces/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ title }) })
}

export function removeWorkspace(id: string): Promise<void> {
  return request(`/api/workspaces/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

/** 新建会话；不传工作区就用服务端启动时的默认工作区。 */
export function createSession(
  payload: { title?: string; workspace_id?: string; mode?: 'chat' | 'agent' } = {},
): Promise<Schemas['SessionSummaryView']> {
  return request('/api/sessions', { method: 'POST', body: JSON.stringify(payload) })
}

/**
 * 提交一条用户消息。
 *
 * `clientRequestId` 是幂等键（§6.1）：**同一次提交的重试必须复用同一个值**（服务端据此判重放），
 * 而"用户又发了一条新消息"必须换新值——两者混了会变成"第二条消息被判成重放，静默丢弃"。
 */
export function submitMessage(
  sessionId: string,
  text: string,
  clientRequestId: string,
  context: SubmissionContext = {},
): Promise<Schemas['CommandResultView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ text, client_request_id: clientRequestId, ...context }),
  })
}

export type SubmissionContext = Pick<components['schemas']['SubmitMessageRequest'],
  'parent_turn_id' | 'expected_branch_id' | 'expected_snapshot_id' | 'workflow' | 'attachments'>

/**
 * 上传一张图片（2026-10-16），返回它的**引用**。
 *
 * 为什么是 base64 的 JSON 而不是 multipart：后者要给运行期（以及打包）多带一个
 * `python-multipart` 依赖，而 4 MB 上限下 base64 的开销无所谓；契约与生成类型也干净。
 * **媒体类型不由页面声明**：服务端按魔数判定（`报告.png` 完全可能是 HTML），所以这里只发字节。
 */
/**
 * 一张已上传图片的**取回地址**（2026-10-16）。
 *
 * 引用是相对路径（含 `/`），所以走**查询参数**而不是路径参数——路径参数里的 `/` 会被当成路由分隔符。
 * 地址里带会话 id：服务端只认**本会话**的引用（跨会话取不到，这是它的边界之一）。
 */
export function attachmentUrl(sessionId: string, attachmentId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/attachments?attachment_id=${encodeURIComponent(attachmentId)}`
}

export function uploadAttachment(
  sessionId: string,
  dataBase64: string,
): Promise<Schemas['AttachmentView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/attachments`, {
    method: 'POST',
    body: JSON.stringify({ data_base64: dataBase64 }),
  })
}

/**
 * 取消当前会话在跑的任务（§17.4，T60 起公开）。
 *
 * **幂等**：没有可取消的任务时是空操作，返回当时的结局，不报错、也不产生事件。
 * **不撤销**已经发生的副作用（改过的文件、发出去的请求都不回滚）；**不释放**会话锁——
 * 释放的前提是这一轮真的收敛（模型流被打断、本轮落到 `cancelled`），所以停止是"等它收敛"。
 * 它**不带幂等键**：重复调用本来就是空操作，要求一个用不上的键只会让人以为取消会被去重。
 */
/** 人工核对结案（§17.1 唯一例外）：`needs_review` → `completed` / `cancelled`。 */
export function closeTaskReview(
  jobId: string,
  outcome: 'completed' | 'cancelled',
): Promise<{ job_id: string; state: string }> {
  return request(`/api/tasks/${encodeURIComponent(jobId)}/review`, {
    method: 'POST',
    body: JSON.stringify({ outcome }),
  })
}

export function cancelTask(sessionId: string, reason = ''): Promise<Schemas['CommandResultView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function getConversationMap(sessionId: string, query = '') {
  return request<components['schemas']['ConversationMapView']>(
    `/api/sessions/${encodeURIComponent(sessionId)}/conversation-map?query=${encodeURIComponent(query)}`,
  )
}

export function getTurnSnapshot(sessionId: string, turnId: string, includeAncestors = false) {
  return request<components['schemas']['SessionSnapshot']>(
    `/api/sessions/${encodeURIComponent(sessionId)}/turns/${encodeURIComponent(turnId)}?include_ancestors=${String(includeAncestors)}`,
  )
}

export function selectBranch(sessionId: string, branchId: string) {
  return request<components['schemas']['SessionSnapshot']>(
    `/api/sessions/${encodeURIComponent(sessionId)}/branches/${encodeURIComponent(branchId)}/select`,
    { method: 'POST' },
  )
}

export function arrangeBranch(sessionId: string, branchId: string,
  payload: components['schemas']['ArrangeBranchRequest']) {
  return request<components['schemas']['ConversationMapView']>(
    `/api/sessions/${encodeURIComponent(sessionId)}/branches/${encodeURIComponent(branchId)}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
  )
}

/** 审批决定；`edit` 必须带 `edited_arguments`，非 `edit` 带参数会被服务端拒绝（§15.7）。 */
export function resolveApproval(
  sessionId: string,
  approvalId: string,
  payload: {
    decision: 'approve' | 'edit' | 'reject'
    client_request_id: string
    edited_arguments?: Record<string, unknown>
    reason?: string
    confirm_recursive?: boolean
  },
): Promise<Schemas['CommandResultView']> {
  return request(
    `/api/sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(approvalId)}`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
}

// ---- 模型连接（§6.8）--------------------------------------------------------

export function listModelConnections(): Promise<ModelConnectionList> {
  return request<ModelConnectionList>('/api/model-connections')
}

export function createModelConnection(body: ModelConnectionInput): Promise<ModelConnectionDto> {
  return request<ModelConnectionDto>('/api/model-connections', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}

/** 编辑必须带 `expected_version`：服务端拒绝"拿着旧版本覆盖别人改动"（§6.1）。 */
export function updateModelConnection(
  connectionId: string,
  body: ModelConnectionPatch,
): Promise<ModelConnectionDto> {
  return request<ModelConnectionDto>(`/api/model-connections/${encodeURIComponent(connectionId)}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  })
}

/** 删除 = **停用**：历史与用量不删（§6.8）。 */
export function retireModelConnection(
  connectionId: string,
  expectedVersion?: number,
): Promise<ModelConnectionDto> {
  const query = expectedVersion === undefined ? '' : `?expected_version=${expectedVersion}`
  return request<ModelConnectionDto>(
    `/api/model-connections/${encodeURIComponent(connectionId)}${query}`,
    { method: 'DELETE' },
  )
}

/** 由用户主动触发的一次探测（会产生少量调用）：分项报告鉴权/聊天/工具/流式。 */
export function testModelConnection(connectionId: string): Promise<ModelConnectionDto> {
  return request<ModelConnectionDto>(
    `/api/model-connections/${encodeURIComponent(connectionId)}/test`,
    { method: 'POST' },
  )
}

/** 写入选择时必须回传"我看到的当前选择"（§6.1）：对不上服务端返回 409，而不是覆盖别人。 */
export function selectDefaultModel(
  connectionId: string,
  expectation: components['schemas']['SelectionExpectation'],
  expectedVersion: number,
): Promise<ModelConnectionDto> {
  return request<ModelConnectionDto>('/api/model-connections/default', {
    method: 'PUT',
    body: JSON.stringify({
      connection_id: connectionId,
      expected_selection: expectation,
      expected_version: expectedVersion,
    }),
  })
}

/** 只写"下一轮"的设置；返回新快照，页面直接显示结果。 */
export function selectSessionModel(
  sessionId: string,
  connectionId: string | null,
  expectation: components['schemas']['SelectionExpectation'],
  expectedVersion: number | null,
): Promise<components['schemas']['SessionSnapshot']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/model`, {
    method: 'PUT',
    body: JSON.stringify({
      connection_id: connectionId,
      expected_selection: expectation,
      expected_version: expectedVersion,
    }),
  })
}

// ---- 权限：工作区外路径档位与授权（T38；§1.5）--------------------------------

/**
 * 当前档位与授权条目。
 *
 * 没有可用设置库时**不报错**：服务端返回出厂默认档位并置 `store_unavailable=true`，
 * 设置页据此如实说明"这里改了也存不下来"，而不是整页打不开。
 */
export function getPermissions(): Promise<PermissionsDto> {
  return request<PermissionsDto>('/api/permissions')
}

export function getPreferences(): Promise<PreferencesDto> {
  return request('/api/preferences')
}

// ---- 能力开关（T56；§16.1、§16.6）------------------------------------------

export type CapabilitiesDto = components['schemas']['CapabilitiesView']
export type CapabilityStatusDto = components['schemas']['CapabilityStatusView']

/**
 * 能力开关表与每个能力的可用性（原因 + 可照做的下一步）。
 *
 * 服务端**每次现算**（取决于开关与这台机器上装没装 git），所以这里不做长缓存：打开开关或装好依赖
 * 后刷新即得，页面不保存一份可能已经过期的结论。
 */
export function getCapabilities(): Promise<CapabilitiesDto> {
  return request<CapabilitiesDto>('/api/capabilities')
}

/**
 * 改能力开关（§16.1）：**保存即生效、作用于下一轮**（改完不用重启）。
 *
 * 只报告**这次动过的那几项**（服务端合并式写入），其余保持原样。可开关集由服务端枚举
 * （`CapabilitiesView.switchable`），页面不自己拼能力名——打错名字会拿到 422，理由里带着可开关集；
 * 没有设置库时是 503（改动存不下来）。
 */
export function setCapabilities(changes: Record<string, boolean>): Promise<CapabilitiesDto> {
  return request<CapabilitiesDto>('/api/capabilities', {
    method: 'PUT',
    body: JSON.stringify({ changes }),
  })
}

// ---- MCP 外部工具（2026-10-20；契约 §6.13）----------------------------------

export type McpDto = components['schemas']['McpView']
export type McpServerDto = components['schemas']['McpServerView']

/**
 * MCP 面板数据：每台 server 的状态、工具与规模统计。
 *
 * 服务端**每次现算**（含重读 `mcp.json` 得出的 `config_error`），所以这里不做长缓存：
 * 改完文件刷新就能看到"第几行错了"，而不是要等重启。
 */
export function getMcp(): Promise<McpDto> {
  return request<McpDto>('/api/mcp')
}

/**
 * 改一台 server 的开关或工具名单：只带**这次动过的键**。
 *
 * 显式传 `enabled_tools: null` 表示"取消白名单"；不传表示"不改它"（两条语义不同）。
 */
export function setMcpServer(name: string, changes: Record<string, unknown>): Promise<McpDto> {
  return request<McpDto>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: 'PUT',
    body: JSON.stringify(changes),
  })
}

/** 重读 `mcp.json`（手改了文件之后用）：会断开配置里已删除的 server。 */
export function reloadMcp(): Promise<McpDto> {
  return request<McpDto>('/api/mcp/reload', { method: 'POST' })
}

export type McpServerCreateInput = {
  name: string
  command: string
  args: string[]
  env: Record<string, string>
}

/**
 * 新增一台 stdio server：写进 `mcp.json` 并立即重载（同名会被 422 拒绝）。
 *
 * `env` 里的凭据**只写不回读**：响应里没有任何 env 字段。
 */
export function addMcpServer(input: McpServerCreateInput): Promise<McpDto> {
  return request<McpDto>('/api/mcp/servers', {
    method: 'POST',
    body: JSON.stringify(input),
  })
}

// ---- 技能管理（技能管理页，2026-10-16；§16.5）--------------------------------

export type SkillsDto = components['schemas']['SkillsView']
export type SkillEntryDto = components['schemas']['SkillEntryView']

/**
 * 本机发现到的技能与启用状态。
 *
 * 服务端**每次现算**（技能是每轮在发现根里现扫的），所以这里不做长缓存：刚放进去或刚删掉的技能，
 * 下一次打开这一页就该看到真实结果。逐条 `enabled` 是**用户**的选择——库里只记被禁用的那些，
 * 新装/未动过的技能默认可用。
 */
export function getSkills(): Promise<SkillsDto> {
  return request<SkillsDto>('/api/skills')
}

/**
 * 启用/禁用一个技能（§16.5）：**保存即生效、作用于下一轮**。
 *
 * 一次一个（界面就是一个开关）。禁用**不删除任何文件**，只让这个技能对模型不可用；名字不在当前
 * 发现到的技能里会拿到 422，理由里带着可选项；没有设置库时是 503（改动存不下来）。
 */
export function setSkillEnabled(name: string, enabled: boolean): Promise<SkillsDto> {
  return request<SkillsDto>('/api/skills', {
    method: 'PUT',
    body: JSON.stringify({ name, enabled }),
  })
}

/**
 * 添加一个技能发现根（2026-10-20）：**只引用目录，不复制文件**。
 *
 * 只接受绝对路径：相对路径的基准在冻结形态是数据目录、源码形态是配置文件目录，让用户猜基准
 * 只会得到"路径明明对却什么都扫不到"。`~` 由调用方展开成绝对路径再提交。
 */
export function addSkillRoot(path: string): Promise<SkillsDto> {
  return request<SkillsDto>('/api/skills/roots', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
}

/** 移除一个**自己添加**的发现根：不删文件，只是不再扫描。 */
export function removeSkillRoot(path: string): Promise<SkillsDto> {
  return request<SkillsDto>(`/api/skills/roots?path=${encodeURIComponent(path)}`, {
    method: 'DELETE',
  })
}

// ---- 任务中心（T62；§17.6）---------------------------------------------------

export type TasksDto = components['schemas']['TaskCenterView']
export type TaskDto = components['schemas']['TaskView']

/**
 * 当前工作区的任务列表（状态、已运行时长、"在等什么"）。
 *
 * **只读**：服务端只读任务索引 + 会话索引，不读任何会话的 Checkpoint（§17.6），
 * 所以这个请求没有副作用，也不会推进任何一轮。`workspace_id` 省略时列出全部工作区。
 */
export function getTasks(workspaceId: string | null): Promise<TasksDto> {
  const query = workspaceId === null ? '' : `?workspace_id=${encodeURIComponent(workspaceId)}`
  return request<TasksDto>(`/api/tasks${query}`)
}

export type MemoryFilters = NonNullable<paths['/api/data-center/memories']['get']['parameters']['query']>
export type UsageFilters = NonNullable<paths['/api/data-center/usage']['get']['parameters']['query']>
export type ArtifactKind = 'image' | 'file' | 'link'
/** 产物筛选（§15.11.1）：`days` 是扫描窗口，不是展示过滤。 */
export interface ArtifactFilters {
  workspace_id?: string
  kind?: ArtifactKind
  days?: number
  offset?: number
  limit?: number
}
function filterQuery(filters: MemoryFilters | UsageFilters | ArtifactFilters): string {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => { if (value !== undefined && value !== null && value !== '') params.set(key, String(value)) })
  return params.toString()
}
export function getMemoryPage(filters: MemoryFilters): Promise<components['schemas']['MemoryPageView']> {
  return request(`/api/data-center/memories?${filterQuery(filters)}`)
}
export function getUsagePage(filters: UsageFilters): Promise<components['schemas']['UsagePageView']> {
  return request(`/api/data-center/usage?${filterQuery(filters)}`)
}
export function getArtifacts(
  filters: ArtifactFilters,
  refresh = false,
): Promise<components['schemas']['ArtifactsPageView']> {
  const query = filterQuery(filters)
  // 服务端有 60 秒结果缓存；`refresh=true` 跳过它（页面「刷新」按钮用）。
  const suffix = refresh ? `${query === '' ? '' : '&'}refresh=true` : ''
  return request(`/api/data-center/artifacts?${query}${suffix}`)
}

// ---- 侧边会话（§21.7）---------------------------------------------------
/**
 * 从某个会话的某一轮开侧聊。
 *
 * **只传"从哪一轮"**，上下文由服务端读——客户端能传历史就等于能伪造历史。
 */
export function createSideChat(
  sessionId: string,
  originTurnId?: string,
): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/side-chats`, {
    method: 'POST',
    body: JSON.stringify({ origin_turn_id: originTurnId ?? null }),
  })
}

/** 侧聊提升为正式会话（§21.7）：清掉血统，**不改 id、不删历史**。 */
export function promoteSideChat(sessionId: string): Promise<Schemas['SessionSummaryView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/promote`, { method: 'POST' })
}

// ---- 右栏只读面板（§21.6）---------------------------------------------
export function listWorkspaceFiles(
  path = '',
  workspaceId?: string,
  recursive = false,
): Promise<Schemas['WorkspaceFilesView']> {
  const query = new URLSearchParams()
  if (path) query.set('path', path)
  if (workspaceId) query.set('workspace_id', workspaceId)
  if (recursive) query.set('recursive', 'true')
  return request(`/api/workspace/files${query.size === 0 ? '' : `?${query}`}`)
}

/** 按文件名递归搜索工作区（§21.6）：跳过噪声目录，最多 50 条。 */
export function searchWorkspaceFiles(q: string, workspaceId?: string): Promise<Schemas['WorkspaceFilesView']> {
  const query = new URLSearchParams({ q })
  if (workspaceId) query.set('workspace_id', workspaceId)
  return request(`/api/workspace/files/search?${query}`)
}

export function getWorkspaceGitStatus(workspaceId?: string): Promise<Schemas['GitStatusView']> {
  const query = new URLSearchParams()
  if (workspaceId) query.set('workspace_id', workspaceId)
  return request(`/api/workspace/git/status${query.size === 0 ? '' : `?${query}`}`)
}

export function getWorkspaceGitDiff(path: string, workspaceId?: string): Promise<Schemas['GitDiffView']> {
  const query = new URLSearchParams({ path })
  if (workspaceId) query.set('workspace_id', workspaceId)
  return request(`/api/workspace/git/diff?${query}`)
}

/** 产物内容预览（§15.11.3）：只读工作区内、服务端限 256 KB、外链由调用方交浏览器。 */
export function previewArtifact(
  path: string,
  workspaceId?: string,
  scope: 'workspace' | 'attachments' = 'workspace',
): Promise<components['schemas']['ArtifactPreviewView']> {
  const query = new URLSearchParams({ path })
  if (workspaceId) query.set('workspace_id', workspaceId)
  if (scope !== 'workspace') query.set('scope', scope)
  return request(`/api/data-center/artifacts/preview?${query}`)
}

// ---- 定时任务（§21.5，2026-10-22 J2）-----------------------------------
export function listCronJobs(): Promise<Schemas['CronJobsView']> {
  return request('/api/cron/jobs')
}

/** `next_run_at` 故意不在入参里：排期只由服务端算（§21.5）。 */
export function createCronJob(payload: Schemas['CronJobCreateRequest']): Promise<Schemas['CronJobView']> {
  return request('/api/cron/jobs', { method: 'POST', body: JSON.stringify(payload) })
}

export function updateCronJob(
  jobId: string,
  payload: Schemas['CronJobUpdateRequest'],
): Promise<Schemas['CronJobView']> {
  return request(`/api/cron/jobs/${encodeURIComponent(jobId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export async function deleteCronJob(jobId: string): Promise<void> {
  await request(`/api/cron/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' })
}

export function runCronJobNow(jobId: string): Promise<Schemas['CronRunView']> {
  return request(`/api/cron/jobs/${encodeURIComponent(jobId)}/run`, { method: 'POST' })
}

export function listCronRuns(jobId: string, offset = 0, limit = 20): Promise<Schemas['CronRunsPageView']> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return request(`/api/cron/jobs/${encodeURIComponent(jobId)}/runs?${query}`)
}

export function savePreferences(values: PreferenceValues, expectedVersion: number): Promise<PreferencesDto> {
  return request('/api/preferences', {
    method: 'PUT', body: JSON.stringify({ values, expected_version: expectedVersion }),
  })
}

export type MemoryProbeDto = components['schemas']['MemoryProbeResult']
export function testMemoryPreferences(values: PreferenceValues, expectedVersion: number): Promise<MemoryProbeDto> {
  return request('/api/preferences/memory/test', {
    method: 'POST', body: JSON.stringify({ values, expected_version: expectedVersion }),
  })
}

export type WebSearchKeyDto = components['schemas']['WebSearchKeyView']

/** 需要密钥的检索后端：两个后端各占一条密钥，页面按当前选中的后端读写对应那条。 */
export type WebSearchProvider = 'tavily' | 'deepseek'

/**
 * 联网检索的服务凭据状态。
 *
 * 返回值里**没有密钥**（连掩码都没有）：页面只据此决定是提示"去录入"还是"可以开始检索"，
 * 以及是否存在设置库里（`store`）。读取失败时服务端会如实说明，不假装"已配置"。
 *
 * `provider` 缺省 `tavily`：升级前只有这一个后端，不传参的调用语义不变。
 */
export function getWebSearchKey(provider: WebSearchProvider = 'tavily'): Promise<WebSearchKeyDto> {
  return request<WebSearchKeyDto>(`/api/web-search/credential?provider=${provider}`)
}

/**
 * 写入（覆盖）指定后端凭据：**保存即生效**，下一轮检索就用它。
 *
 * 只写不回读：要替换就再写一次，要清空走 `clearWebSearchKey`——"清空"与"改成空白"是两种意图，
 * 服务端也只接受前者来表达删除（空白密钥是 422）。
 */
export function setWebSearchKey(apiKey: string, provider: WebSearchProvider = 'tavily'): Promise<WebSearchKeyDto> {
  return request<WebSearchKeyDto>(`/api/web-search/credential?provider=${provider}`, {
    method: 'PUT',
    body: JSON.stringify({ api_key: apiKey }),
  })
}

/** 删除已保存的凭据（幂等）。删掉之后旧环境变量仍然有效，返回的 `source` 会如实反映这一点。 */
export function clearWebSearchKey(provider: WebSearchProvider = 'tavily'): Promise<WebSearchKeyDto> {
  return request<WebSearchKeyDto>(`/api/web-search/credential?provider=${provider}`, { method: 'DELETE' })
}

/** 对象存储密钥状态（2026-10-20）：只报"有没有"，不回读密钥。 */
export function getOssCredential(): Promise<components['schemas']['OssCredentialView']> {
  return request<components['schemas']['OssCredentialView']>('/api/attachments/oss/credential')
}

/** 保存（覆盖）Access Key / Secret Key；写入即整体替换，空值由服务端拒绝。 */
export function setOssCredential(
  accessKey: string,
  secretKey: string,
): Promise<components['schemas']['OssCredentialView']> {
  return request<components['schemas']['OssCredentialView']>('/api/attachments/oss/credential', {
    method: 'PUT',
    body: JSON.stringify({ access_key: accessKey, secret_key: secretKey }),
  })
}

/** 删除已保存的对象存储密钥（幂等）。 */
export function clearOssCredential(): Promise<components['schemas']['OssCredentialView']> {
  return request<components['schemas']['OssCredentialView']>('/api/attachments/oss/credential', {
    method: 'DELETE',
  })
}

/** 测试对象存储：上传一张 1×1 测试图，再检查公开地址能否访问。 */
export function testOssStorage(): Promise<components['schemas']['OssTestResult']> {
  return request<components['schemas']['OssTestResult']>('/api/attachments/oss/test', { method: 'POST' })
}

/** 切换档位。改动**作用于下一轮**：正在跑或等待审批的那些运行已按当时的档位判定完。 */
export function setPermissionsMode(mode: ExternalPathsModeDto): Promise<PermissionsDto> {
  return request<PermissionsDto>('/api/permissions', {
    method: 'PUT',
    body: JSON.stringify({ mode }),
  })
}

/** 给一个工作区外目录授权；同一目标重复授权是**改写**级别，不是加两行。 */
export function grantExternalPath(
  path: string,
  access: ExternalPathAccessDto,
): Promise<PermissionsDto> {
  return request<PermissionsDto>('/api/permissions/grants', {
    method: 'POST',
    body: JSON.stringify({ path, access }),
  })
}

/**
 * 撤销一条授权；**不存在也算成功**（幂等）。
 *
 * 路径走查询参数而不是请求体：它是这条记录的自然键（含层级分隔符），URL 编码即可原样送达。
 */
export function revokeExternalPath(path: string): Promise<PermissionsDto> {
  return request<PermissionsDto>(`/api/permissions/grants?path=${encodeURIComponent(path)}`, {
    method: 'DELETE',
  })
}

export function editMcpServer(name: string, changes: Record<string, unknown>): Promise<McpDto> {
  return request<McpDto>(`/api/mcp/servers/${encodeURIComponent(name)}/spec`, {
    method: 'PUT', body: JSON.stringify(changes),
  })
}

export function deleteMcpServer(name: string): Promise<McpDto> {
  return request<McpDto>(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' })
}
