// 页面状态层的语义测试：stub 掉 fetch，只验状态迁移（不渲染设置页）。
//
// 守四件事：读失败不清空已有数据；探测只标一条并写回结果；版本冲突**不自动重发**；停用后就地移除。

import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HealthDto, ModelConnectionDto } from '../../api/client'
import { useModelSettings } from './useModelSettings'

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

function connection(overrides: Partial<ModelConnectionDto> = {}): ModelConnectionDto {
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
    // 2026-10-16：连接级"支持图片输入"（服务端一定会回/收这一位，夹具照实写）。
    supports_vision: false,
    auth_mode: 'api_key',
    key_status: 'configured',
    is_default: true,
    config_version: '1',
    status: 'unverified',
    transport: 'https',
    capabilities: [{ capability: 'chat', result: 'unknown' }],
    ...overrides,
  }
}

const health = {
  status: 'ok',
  schema_version: 3,
  model_configured: true,
  model_name: 'deepseek-flash',
  credential_store: 'os',
  credential_note: null,
  storage: 'sqlite',
  static_frontend: true,
} as HealthDto

/** 按 `METHOD url` 路由的 fetch 假实现；返回调用记录。处理函数可以是"挂住的" Promise。 */
function stubFetch(routes: Record<string, () => Response | Promise<Response>>) {
  const calls: { key: string; body: unknown }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const key = `${init.method ?? 'GET'} ${url}`
      calls.push({ key, body: init.body ? JSON.parse(String(init.body)) : null })
      const handler = routes[key]
      if (!handler) throw new Error(`没有为 ${key} 准备响应`)
      return handler()
    }),
  )
  return calls
}

/** 完整的连接请求体：契约要求这些字段都在。 */
function body(overrides: Record<string, unknown> = {}) {
  return {
    display_name: '新连接',
    base_url: 'https://api.example.com/v1',
    model: 'mid-model',
    context_window_tokens: 32000,
    max_tokens: 1000,
    temperature: 0.2,
    timeout_seconds: 120,
    retry_count: 2,
    streaming: false,
    // 2026-10-16：连接级"支持图片输入"（服务端一定会回/收这一位，夹具照实写）。
    supports_vision: false,
    auth_mode: 'api_key' as const,
    allow_plaintext_http: false,
    ...overrides,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useModelSettings', () => {
  it('loads connections and the credential store state', async () => {
    stubFetch({
      'GET /api/model-connections': () => json(200, [connection()]),
      'GET /api/health': () => json(200, health),
    })

    const { result } = renderHook(() => useModelSettings())

    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.connections).toHaveLength(1)
    expect(result.current.health?.credential_store).toBe('os')
  })

  it('keeps the list visible when a reload fails', async () => {
    let failing = false
    stubFetch({
      'GET /api/model-connections': () =>
        failing ? json(500, { detail: '内部错误' }) : json(200, [connection()]),
      'GET /api/health': () => json(200, health),
    })

    const { result } = renderHook(() => useModelSettings())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    failing = true
    await act(async () => {
      await result.current.reload()
    })

    expect(result.current.connections).toHaveLength(1) // 不闪成空白
    expect(result.current.error?.detail).toContain('内部错误')
  })

  it('marks only the probed connection as testing and writes the result back', async () => {
    stubFetch({
      'GET /api/model-connections': () => json(200, [connection()]),
      'GET /api/health': () => json(200, health),
      'POST /api/model-connections/mc-1/test': () => json(200, connection({ status: 'verified' })),
    })

    const { result } = renderHook(() => useModelSettings())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.test('mc-1')
    })

    expect(result.current.connections[0]?.status).toBe('verified')
    expect(result.current.testingId).toBeNull() // 结束后不再显示"检测中"
  })

  it('treats a version conflict as "refresh first" instead of retrying', async () => {
    stubFetch({
      'GET /api/model-connections': () => json(200, [connection()]),
      'GET /api/health': () => json(200, health),
      'PATCH /api/model-connections/mc-1': () =>
        json(409, {
          error: {
            code: 'MODEL_SETTINGS_CONFLICT',
            category: 'configuration',
            severity: 'error',
            title: '这条连接已被改动',
            message: '你的改动基于一个旧版本，因此没有覆盖当前设置。',
            hint: '刷新这条连接拿到最新版本。',
            retryable: false,
            diagnostic_id: 'x',
            occurred_at: '2026-09-24T00:00:00Z',
            source: 'settings',
          },
        }),
    })

    const { result } = renderHook(() => useModelSettings())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.save(
        {
          display_name: '改名',
          base_url: 'https://api.deepseek.com',
          model: 'deepseek-flash',
          context_window_tokens: 65536,
          max_tokens: 4096,
          temperature: 0.2,
          timeout_seconds: 120,
          retry_count: 2,
          streaming: false,
          supports_vision: false,
          auth_mode: 'api_key',
          allow_plaintext_http: false,
        },
        { connectionId: 'mc-1' },
      )
    })

    expect(result.current.conflict?.code).toBe('MODEL_SETTINGS_CONFLICT')
    expect(result.current.error).toBeNull() // 冲突不是"请求失败"
  })

  it('separates "creating" from "idle" so a double click cannot create two connections', async () => {
    let release: (() => void) | null = null
    stubFetch({
      'GET /api/model-connections': () => json(200, []),
      'GET /api/health': () => json(200, health),
      // 挂住这个请求：模拟"正在飞"，此时页面必须知道自己在忙。
      'POST /api/model-connections': () =>
        new Promise<Response>((resolve) => {
          release = () => resolve(json(200, connection({ display_name: '新连接' })))
        }),
    })

    const { result } = renderHook(() => useModelSettings())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    let pending: Promise<unknown> = Promise.resolve()
    act(() => {
      pending = result.current.save(body())
    })

    await waitFor(() => expect(result.current.creating).toBe(true))
    // 关键：新增时**没有 id**，所以 `busyId` 仍是 null——只看它的话"新增中"和"空闲"一模一样，
    // 保存按钮就不会禁用，连点两次会建出两条一样的连接（复审实测）。
    expect(result.current.busyId).toBeNull()

    await act(async () => {
      release?.()
      await pending
    })
    expect(result.current.creating).toBe(false)
    expect(result.current.connections).toHaveLength(1)
  })

  it('sends the default it saw plus the target version, so two pages cannot overwrite each other', async () => {
    const calls = stubFetch({
      'GET /api/model-connections': () =>
        json(200, [
          connection({ connection_id: 'mc-a', is_default: true, config_version: '3' }),
          connection({ connection_id: 'mc-b', config_version: '7' }),
        ]),
      'GET /api/health': () => json(200, health),
      'PUT /api/model-connections/default': () =>
        json(200, connection({ connection_id: 'mc-b', config_version: '7', is_default: true })),
    })

    const { result } = renderHook(() => useModelSettings())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.selectDefault('mc-b')
    })

    // 两样都必须在请求里：观测（当前默认是 mc-a@3）与目标版本（mc-b@7）。
    // 少了任意一样，服务端就没法判断"这个页面的副本是不是过期了"（§6.1）。
    expect(calls.at(-1)).toEqual({
      key: 'PUT /api/model-connections/default',
      body: {
        connection_id: 'mc-b',
        expected_selection: { connection_id: 'mc-a', config_version: 3 },
        expected_version: 7,
      },
    })
  })

  it('removes a retired connection from the list', async () => {
    stubFetch({
      'GET /api/model-connections': () => json(200, [connection()]),
      'GET /api/health': () => json(200, health),
      'DELETE /api/model-connections/mc-1': () => json(200, connection()),
    })

    const { result } = renderHook(() => useModelSettings())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.retire('mc-1')
    })

    expect(result.current.connections).toEqual([])
  })
})
