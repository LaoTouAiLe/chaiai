// 「设置 → 权限」的页面状态（T38）：组件只管画，这里管"什么时候重载、失败怎么呈现"。
//
// 与 `useModelSettings` 同一条骨架（读失败不清空、组件卸载后不再写状态、忙态防双击），
// 但写入口径不一样：
// 1. **读失败不清空已有数据**：网络抖动不该把权限分区闪成空白，只把错误挂出来；
// 2. **档位与授权都返回完整视图**：服务端每次写都回"改完后的整个权限视图"，这里直接替换，
//    不需要再取一次——界面上的"作用于下一轮"说明跟着视图一起刷新；
// 3. **写失败不改变当前视图**：按钮解除忙态、错误挂出来，用户看到的档位与授权仍是改之前的样子。

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, getPermissions, grantExternalPath, revokeExternalPath, setPermissionsMode } from '../../api/client'
import type { ExternalPathAccessDto, ExternalPathsModeDto, PermissionsDto } from '../../api/client'

export interface PermissionsState {
  status: 'loading' | 'ready' | 'error'
  permissions: PermissionsDto | null
  /** 最近一次失败；成功一次就清掉。 */
  error: ApiError | null
  /** 正在提交的档位或授权（按钮据此禁用，防双击）。 */
  busy: boolean
}

export interface PermissionsActions {
  reload: () => Promise<void>
  setMode: (mode: ExternalPathsModeDto) => Promise<PermissionsDto | null>
  grant: (path: string, access: ExternalPathAccessDto) => Promise<PermissionsDto | null>
  revoke: (path: string) => Promise<PermissionsDto | null>
}

export type Permissions = PermissionsState & PermissionsActions

function asApiError(caught: unknown): ApiError {
  return caught instanceof ApiError
    ? caught
    : new ApiError({ status: 0, code: null, message: '请求失败', detail: String(caught) })
}

export function usePermissions(options: { autoLoad?: boolean } = {}): Permissions {
  const autoLoad = options.autoLoad ?? true
  const [state, setState] = useState<PermissionsState>({
    status: 'loading',
    permissions: null,
    error: null,
    busy: false,
  })

  // 组件卸载后不再写状态：写请求可能比页面活得久（用户已经关了设置页）。
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const patch = useCallback((next: Partial<PermissionsState>) => {
    if (!alive.current) return
    setState((current) => ({ ...current, ...next }))
  }, [])

  const replaceView = useCallback((view: PermissionsDto) => {
    patch({ permissions: view })
  }, [patch])

  const reload = useCallback(async () => {
    try {
      const view = await getPermissions()
      patch({ status: 'ready', permissions: view, error: null })
    } catch (caught) {
      // 读失败**不清空**已有数据：只把错误挂出来，权限分区不闪成空白。
      patch({ status: 'error', error: asApiError(caught) })
    }
  }, [patch])

  useEffect(() => {
    if (!autoLoad) return
    void reload()
  }, [autoLoad, reload])

  const run = useCallback(
    async (action: () => Promise<PermissionsDto>): Promise<PermissionsDto | null> => {
      patch({ busy: true, error: null })
      try {
        const view = await action()
        patch({ busy: false, error: null })
        return view
      } catch (caught) {
        // 写失败**不改变当前视图**：按钮解除忙态、错误挂出来，界面仍是改之前的样子。
        patch({ busy: false, error: asApiError(caught) })
        return null
      }
    },
    [patch],
  )

  const setMode = useCallback<PermissionsActions['setMode']>(
    async (mode) => {
      const view = await run(() => setPermissionsMode(mode))
      if (view) replaceView(view)
      return view
    },
    [replaceView, run],
  )

  const grant = useCallback<PermissionsActions['grant']>(
    async (path, access) => {
      const view = await run(() => grantExternalPath(path, access))
      if (view) replaceView(view)
      return view
    },
    [replaceView, run],
  )

  const revoke = useCallback<PermissionsActions['revoke']>(
    async (path) => {
      const view = await run(() => revokeExternalPath(path))
      if (view) replaceView(view)
      return view
    },
    [replaceView, run],
  )

  return { ...state, reload, setMode, grant, revoke }
}
