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
export function listSessions(workspaceId?: string): Promise<Schemas['SessionSummaryView'][]> {
  const query = workspaceId === undefined ? '' : `?workspace_id=${encodeURIComponent(workspaceId)}`
  return request(`/api/sessions${query}`)
}

/** 新建会话；不传工作区就用服务端启动时的默认工作区。 */
export function createSession(
  payload: { title?: string; workspace_id?: string } = {},
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
): Promise<Schemas['CommandResultView']> {
  return request(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: 'POST',
    body: JSON.stringify({ text, client_request_id: clientRequestId }),
  })
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
