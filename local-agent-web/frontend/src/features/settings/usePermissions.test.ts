// 权限状态层的语义测试（T38）：stub 掉 fetch，只验状态迁移（不渲染设置页）。
//
// 守三件事：读失败不清空已有视图；写成功用服务端回的**完整视图**替换（不再取一次）；
// 写失败**不改动当前视图**、也不假装保存成功。

import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PermissionsDto } from '../../api/client'
import { usePermissions } from './usePermissions'

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function permissions(overrides: Partial<PermissionsDto> = {}): PermissionsDto {
  return {
    mode: 'whitelist',
    default_mode: 'whitelist',
    grants: [],
    store_unavailable: false,
    ...overrides,
  }
}

/** 按 `METHOD url` 路由的 fetch 假实现；返回调用记录。 */
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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('usePermissions', () => {
  it('loads the mode, the factory default and the grants', async () => {
    stubFetch({
      'GET /api/permissions': () =>
        json(
          200,
          permissions({
            mode: 'allow',
            default_mode: 'whitelist',
            grants: [{ path: 'E:/design-assets', access: 'read' }],
          }),
        ),
    })

    const { result } = renderHook(() => usePermissions())

    await waitFor(() => expect(result.current.status).toBe('ready'))
    expect(result.current.permissions?.mode).toBe('allow')
    // 出厂默认与当前档位是两件事：默认来自 config.yaml，不因为用户改过档位就跟着变。
    expect(result.current.permissions?.default_mode).toBe('whitelist')
    expect(result.current.permissions?.grants).toHaveLength(1)
  })

  it('keeps the view visible when a reload fails', async () => {
    let failing = false
    stubFetch({
      'GET /api/permissions': () =>
        failing ? json(500, { detail: '内部错误' }) : json(200, permissions({ mode: 'deny' })),
    })

    const { result } = renderHook(() => usePermissions())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    failing = true
    await act(async () => {
      await result.current.reload()
    })

    expect(result.current.permissions?.mode).toBe('deny') // 不闪成空白
    expect(result.current.error?.detail).toContain('内部错误')
  })

  it('sends the chosen mode and adopts the view the server returns', async () => {
    const calls = stubFetch({
      'GET /api/permissions': () => json(200, permissions()),
      'PUT /api/permissions': () => json(200, permissions({ mode: 'allow' })),
    })

    const { result } = renderHook(() => usePermissions())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.setMode('allow')
    })

    expect(calls.at(-1)).toEqual({ key: 'PUT /api/permissions', body: { mode: 'allow' } })
    expect(result.current.permissions?.mode).toBe('allow')
  })

  it('sends the path and level verbatim and adopts the returned view', async () => {
    const calls = stubFetch({
      'GET /api/permissions': () => json(200, permissions()),
      'POST /api/permissions/grants': () =>
        json(200, permissions({ grants: [{ path: 'D:\\shared\\data', access: 'read_write' }] })),
    })

    const { result } = renderHook(() => usePermissions())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.grant('D:\\shared\\data', 'read_write')
    })

    expect(calls.at(-1)).toEqual({
      key: 'POST /api/permissions/grants',
      body: { path: 'D:\\shared\\data', access: 'read_write' },
    })
    expect(result.current.permissions?.grants).toEqual([
      { path: 'D:\\shared\\data', access: 'read_write' },
    ])
  })

  it('revokes through the query parameter, so a path with separators survives intact', async () => {
    const calls = stubFetch({
      'GET /api/permissions': () =>
        json(200, permissions({ grants: [{ path: 'E:/design-assets', access: 'read' }] })),
      'DELETE /api/permissions/grants?path=E%3A%2Fdesign-assets': () => json(200, permissions()),
    })

    const { result } = renderHook(() => usePermissions())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    await act(async () => {
      await result.current.revoke('E:/design-assets')
    })

    expect(calls.at(-1)?.key).toBe('DELETE /api/permissions/grants?path=E%3A%2Fdesign-assets')
    expect(result.current.permissions?.grants).toEqual([])
  })

  it('leaves the view untouched when a write fails, instead of looking saved', async () => {
    stubFetch({
      'GET /api/permissions': () => json(200, permissions({ mode: 'deny' })),
      'PUT /api/permissions': () =>
        json(503, {
          error: {
            code: 'MODEL_SETTINGS_INVALID',
            category: 'configuration',
            severity: 'error',
            title: '设置库不可用',
            message: '没有可用的设置库，这次改动没有保存。',
            hint: '检查设置库是否可用后重试。',
            retryable: false,
            diagnostic_id: 'x',
            occurred_at: '2026-09-30T00:00:00Z',
            source: 'settings',
          },
        }),
    })

    const { result } = renderHook(() => usePermissions())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    let outcome: PermissionsDto | null = null
    await act(async () => {
      outcome = await result.current.setMode('allow')
    })

    expect(outcome).toBeNull()
    expect(result.current.permissions?.mode).toBe('deny') // 仍是改之前的样子
    expect(result.current.error?.message).toContain('没有保存')
    expect(result.current.busy).toBe(false)
  })

  it('reports itself busy while a write is still in flight', async () => {
    let release: (() => void) | null = null
    stubFetch({
      'GET /api/permissions': () => json(200, permissions()),
      'PUT /api/permissions': () =>
        new Promise<Response>((resolve) => {
          release = () => resolve(json(200, permissions({ mode: 'deny' })))
        }),
    })

    const { result } = renderHook(() => usePermissions())
    await waitFor(() => expect(result.current.status).toBe('ready'))

    let pending: Promise<unknown> = Promise.resolve()
    act(() => {
      pending = result.current.setMode('deny')
    })

    // 忙态必须起来：否则连点两次"放行"会发两次写请求（档位写入不是幂等的语义，只是最后写赢）。
    await waitFor(() => expect(result.current.busy).toBe(true))

    await act(async () => {
      release?.()
      await pending
    })
    expect(result.current.busy).toBe(false)
    expect(result.current.permissions?.mode).toBe('deny')
  })
})