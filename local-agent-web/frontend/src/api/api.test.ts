// 传输客户端与适配器：只测**映射与失败处理**，不测服务端行为（那些在后端测试里）。
//
// 用 stub 掉的 `fetch`：本文件不联网，也不需要 dev server 在跑。

import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HealthDto, ModelConnectionDto, ModelConnectionInput, PermissionsDto } from './client'
import {
  ApiError,
  createModelConnection,
  listModelConnections,
  newRequestId,
  selectSessionModel,
  updateModelConnection,
} from './client'
import { contextWindowLabel, toConnectionView, toModelMenuItem, toPermissionsView, toSettingsView } from './viewModels'

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function connectionDto(overrides: Partial<ModelConnectionDto> = {}): ModelConnectionDto {
  return {
    connection_id: 'mc-1',
    display_name: '主力模型',
    base_url: 'https://api.deepseek.com',
    provider: 'openai_compatible',
    model_id: 'deepseek-flash',
    context_window_tokens: 65536,
    max_tokens: 4096,
    temperature: 0.2,
    timeout_seconds: 120,
    retry_count: 2,
    streaming: false,
    auth_mode: 'api_key',
    key_status: 'configured',
    is_default: true,
    config_version: '1',
    status: 'unverified',
    transport: 'https',
    capabilities: [
      { capability: 'chat', result: 'unknown' },
      { capability: 'tool_calling', result: 'unknown' },
      { capability: 'streaming', result: 'unknown' },
    ],
    ...overrides,
  }
}

/** 完整的连接请求体：契约要求这些字段都在（页面本来就会把整份表单发上来）。 */
function connectionBody(overrides: Partial<ModelConnectionInput> = {}): ModelConnectionInput {
  return {
    display_name: '局域网网关',
    base_url: 'https://api.example.com/v1',
    model: 'mid-model',
    context_window_tokens: 32000,
    max_tokens: 1000,
    temperature: 0.2,
    timeout_seconds: 120,
    retry_count: 2,
    streaming: false,
    auth_mode: 'api_key',
    allow_plaintext_http: false,
    ...overrides,
  }
}

const healthDto: HealthDto = {
  status: 'ok',
  schema_version: 3,
  model_configured: true,
  model_name: 'deepseek-flash',
  credential_store: 'os',
  credential_note: null,
  storage: 'sqlite',
  static_frontend: true,
} as HealthDto

function permissionsDto(overrides: Partial<PermissionsDto> = {}): PermissionsDto {
  return {
    mode: 'whitelist',
    default_mode: 'whitelist',
    grants: [],
    store_unavailable: false,
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('API client', () => {
  it('reads the connection list from the relative /api path', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, [connectionDto()]))
    vi.stubGlobal('fetch', fetchMock)

    const list = await listModelConnections()

    expect(list).toHaveLength(1)
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/model-connections')
  })

  it('sends a JSON body with the request id and the current version', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, connectionDto({ config_version: '2' })))
    vi.stubGlobal('fetch', fetchMock)

    await updateModelConnection('mc-1', connectionBody({ display_name: '改名', expected_version: 1 }))

    const [url, init] = fetchMock.mock.calls[0] ?? []
    expect(url).toBe('/api/model-connections/mc-1')
    expect(init.method).toBe('PATCH')
    expect(init.headers['content-type']).toBe('application/json')
    expect(JSON.parse(init.body).expected_version).toBe(1)
  })

  it('turns a business error envelope into a typed error the UI can act on', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(409, {
          error: {
            code: 'MODEL_SETTINGS_CONFLICT',
            category: 'configuration',
            severity: 'error',
            title: '这条连接已被改动',
            message: '你的改动基于一个旧版本，因此没有覆盖当前设置。',
            hint: '刷新这条连接拿到最新版本，再把你的改动重新提交一次。',
            retryable: false,
            retry_condition: '刷新这条连接后重新提交修改。',
            diagnostic_id: 'MODEL_SETTINGS_CONFLICT:abcd',
            occurred_at: '2026-09-24T00:00:00Z',
            source: 'settings',
          },
        }),
      ),
    )

    const error = await updateModelConnection(
      'mc-1',
      connectionBody({ expected_version: 1 }),
    ).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(409)
    expect(apiError.code).toBe('MODEL_SETTINGS_CONFLICT')
    expect(apiError.hint).toContain('刷新')
    expect(apiError.retryable).toBe(false)
  })

  it('keeps transport-level rejections separate from business codes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(403, { detail: 'Host 不在允许列表里：本服务只接受本机访问。' })),
    )

    const error = (await createModelConnection(connectionBody()).catch(
      (caught: unknown) => caught,
    )) as ApiError

    // 传输层拒绝没有业务码：页面只该说"请求没能进入业务逻辑"，不能按错误码分支。
    expect(error.code).toBeNull()
    expect(error.detail).toContain('Host')
  })

  it('mints distinct request ids even without crypto.randomUUID', () => {
    vi.stubGlobal('crypto', {})
    const first = newRequestId()
    const second = newRequestId()

    expect(first).not.toBe(second)
    expect(first.startsWith('req-')).toBe(true)
  })

  it('posts the session model selection and returns the fresh snapshot', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(200, { session_id: 's-1', messages: [], current_task: null }))
    vi.stubGlobal('fetch', fetchMock)

    // 选择写入必须回传"我看到的当前选择"（§6.1）：这里断言它真的进了请求体。
    const snapshot = await selectSessionModel(
      's-1',
      'mc-2',
      { connection_id: null, config_version: null },
      1,
    )

    expect(snapshot.session_id).toBe('s-1')
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/sessions/s-1/model')
    const body = JSON.parse(fetchMock.mock.calls[0]?.[1]?.body)
    expect(body.connection_id).toBe('mc-2')
    expect(body.expected_selection).toEqual({ connection_id: null, config_version: null })
    expect(body.expected_version).toBe(1)
  })
})

describe('view model adapters', () => {
  it('maps the contract spelling to the display spelling without inventing states', () => {
    const view = toConnectionView(
      connectionDto({ key_status: 'session_only', status: 'verified', transport: 'plaintext_http' }),
    )

    expect(view.keyStatus).toBe('session-only')
    expect(view.status).toBe('verified')
    expect(view.transport).toBe('plaintext-http')
    // 适配器不会产出 `testing`：那是页面在"已发出探测、还没回来"时的本地态。
    expect(view.status).not.toBe('testing')
    expect(view.confirmDelete).toBe(false)
    expect(view.pendingApply).toBe(false)
  })

  it('lists the three capabilities in a stable order and defaults missing ones to unknown', () => {
    const view = toConnectionView(
      connectionDto({
        capabilities: [{ capability: 'chat', result: 'passed' }],
      }),
    )

    expect(view.capabilities.map((item) => item.name)).toEqual(['聊天', '工具调用', '流式'])
    expect(view.capabilities.map((item) => item.result)).toEqual(['passed', 'unknown', 'unknown'])
    // 非整千就显示精确值：展示层不去猜一个它没被告知的窗口。
    expect(view.contextWindowLabel).toBe('65536')
  })

  it('takes the credential store state from health so the empty state can warn too', () => {
    const settings = toSettingsView([], {
      ...healthDto,
      credential_store: 'memory',
      credential_note: '系统凭据存储不可用：密钥只在本进程内有效，重启后需要重新录入。',
    })

    expect(settings.connections).toEqual([])
    expect(settings.credentialStore.kind).toBe('unavailable')
    expect(settings.credentialStore.note).toContain('重新录入')
  })

  it('maps the permissions DTO onto the view without inventing a mode', () => {
    const view = toPermissionsView({
      mode: 'allow',
      default_mode: 'whitelist',
      grants: [{ path: 'D:/team/shared', access: 'read_write' }],
      store_unavailable: true,
    })

    expect(view).toEqual({
      mode: 'allow',
      defaultMode: 'whitelist',
      grants: [{ path: 'D:/team/shared', access: 'read_write' }],
      storeUnavailable: true,
    })
  })

  it('keeps permissions null in the settings view until they have been read', () => {
    const settings = toSettingsView([], healthDto)
    expect(settings.permissions).toBeNull()
  })

  it('carries the permissions DTO into the settings view once read', () => {
    const settings = toSettingsView(
      [],
      healthDto,
      undefined,
      permissionsDto({ mode: 'deny', default_mode: 'whitelist', grants: [] }),
    )
    expect(settings.permissions?.mode).toBe('deny')
    expect(settings.permissions?.defaultMode).toBe('whitelist')
  })

  it('formats the context window without pretending to know an exact window it was not given', () => {
    expect(contextWindowLabel(32000)).toBe('32k')
    expect(contextWindowLabel(65536)).toBe('65536')
  })

  it('maps a menu row from the same DTO the settings card uses', () => {
    const item = toModelMenuItem(connectionDto({ display_name: '备用', model_id: 'other' }))

    expect(item).toEqual({ id: 'mc-1', name: '备用', modelId: 'other', status: 'unverified' })
  })
})
