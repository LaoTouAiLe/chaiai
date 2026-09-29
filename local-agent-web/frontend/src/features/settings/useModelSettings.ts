// 模型连接的页面状态（T29A）：组件只管画，这里管"什么时候重载、失败怎么呈现、冲突怎么办"。
//
// 依赖方向是单向的：本模块用 `api/client.ts`（传输）与 `api/viewModels.ts`（展示映射），
// 两个方向都不反过来依赖页面组件——所以它可以被 vitest 直接测（stub fetch），不需要渲染。
//
// 四条语义（对应 §6.8 与 §6.1 的写入口径）：
// 1. **读失败不清空已有数据**：网络抖动不该把用户眼前的列表变成空白，只把错误挂出来；
// 2. **测试连接是"这一条"的状态**：`testingId` 只标一条，页面据此只让那一张卡显示"检测中"；
// 3. **版本冲突要求刷新而不是重试**：409 后不自动重发（重发还是旧版本），只提示并等 `reload()`；
// 4. **停用后就地从列表移除**：列表接口本来也不返回停用的连接，本地过滤与之等价。

import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError, createModelConnection, getHealth, listModelConnections, retireModelConnection, selectDefaultModel, testModelConnection, updateModelConnection } from '../../api/client'
import type { HealthDto, ModelConnectionDto, ModelConnectionInput } from '../../api/client'

/** 服务端的状态码：冲突要"刷新后重试"，与其他失败分开处理。 */
const CONFLICT_CODE = 'MODEL_SETTINGS_CONFLICT'

export interface ModelSettingsState {
  /** `loading` 只在**首次**加载时为真：重载不该让整页回到骨架（刷新时保持可见内容更稳）。 */
  status: 'loading' | 'ready' | 'error'
  connections: readonly ModelConnectionDto[]
  health: HealthDto | null
  /** 最近一次失败；成功一次就清掉。 */
  error: ApiError | null
  /** 正在探测的连接 id（页面只让这一张卡显示"检测中"）。 */
  testingId: string | null
  /** 正在提交的那条连接 id（编辑/探测/停用/设默认都走它）。 */
  busyId: string | null
  /**
   * **新增**是否正在提交。
   *
   * 为什么不能只看 `busyId`：新增时还没有 id，`busyId` 只能是 `null`，而它同时代表"空闲"——
   * 于是保存按钮不禁用，双击就能建出两条一模一样的连接（复审实测）。新增要有自己的忙态。
   */
  creating: boolean
  /** 版本冲突：不自动重试，等用户刷新后再提交。 */
  conflict: ApiError | null
}

export interface ModelSettingsActions {
  reload: () => Promise<void>
  /** 新增（无 `connectionId`）或编辑（有：自动带上当前版本号）。 */
  save: (
    input: ModelConnectionInput,
    options?: { connectionId?: string; apiKey?: string | null },
  ) => Promise<ModelConnectionDto | null>
  test: (connectionId: string) => Promise<ModelConnectionDto | null>
  selectDefault: (
    connectionId: string,
    justSaved?: ModelConnectionDto,
  ) => Promise<ModelConnectionDto | null>
  retire: (connectionId: string) => Promise<boolean>
}

export type ModelSettings = ModelSettingsState & ModelSettingsActions

function asApiError(caught: unknown): ApiError {
  return caught instanceof ApiError
    ? caught
    : new ApiError({ status: 0, code: null, message: '请求失败', detail: String(caught) })
}

export function useModelSettings(options: { autoLoad?: boolean } = {}): ModelSettings {
  const autoLoad = options.autoLoad ?? true
  const [state, setState] = useState<ModelSettingsState>({
    status: 'loading',
    connections: [],
    health: null,
    error: null,
    testingId: null,
    busyId: null,
    creating: false,
    conflict: null,
  })

  // 组件卸载后不再写状态：探测可能比页面活得久（用户已经关了设置页）。
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const patch = useCallback((next: Partial<ModelSettingsState>) => {
    if (!alive.current) return
    setState((current) => ({ ...current, ...next }))
  }, [])

  const replaceOne = useCallback((dto: ModelConnectionDto) => {
    if (!alive.current) return
    setState((current) => ({
      ...current,
      connections: current.connections.map((item) =>
        item.connection_id === dto.connection_id ? dto : item,
      ),
    }))
  }, [])

  const reload = useCallback(async () => {
    try {
      const [connections, health] = await Promise.all([listModelConnections(), getHealth()])
      patch({ status: 'ready', connections, health, error: null, conflict: null })
    } catch (caught) {
      // 读失败**不清空**已有数据：只把错误挂出来，用户看到的列表不闪成空白。
      patch({ status: 'error', error: asApiError(caught) })
    }
  }, [patch])

  useEffect(() => {
    if (!autoLoad) return
    void reload()
  }, [autoLoad, reload])

  const run = useCallback(
    async <T,>(
      target: string | null,
      action: () => Promise<T>,
    ): Promise<T | null> => {
      patch({ busyId: target, conflict: null })
      try {
        const result = await action()
        patch({ busyId: null, error: null })
        return result
      } catch (caught) {
        const error = asApiError(caught)
        // 冲突是"你的副本过期了"，与"请求失败"不是一回事：分开呈现，且不自动重发。
        patch({
          busyId: null,
          error: error.code === CONFLICT_CODE ? null : error,
          conflict: error.code === CONFLICT_CODE ? error : null,
        })
        return null
      }
    },
    [patch],
  )

  const save = useCallback<ModelSettingsActions['save']>(
    async (input, saveOptions = {}) => {
      const { connectionId, apiKey } = saveOptions
      const body = { ...input, api_key: apiKey ?? null }
      if (connectionId === undefined) {
        // 新增的忙态与"正在改哪一条"分开：`busyId` 在这里只能是 `null`，那跟空闲撞了。
        patch({ creating: true })
        const created = await run(null, () => createModelConnection(body))
        patch({ creating: false })
        if (created) {
          if (!alive.current) return created
          setState((current) => ({ ...current, connections: [...current.connections, created] }))
        }
        return created
      }
      const currentVersion = state.connections.find((item) => item.connection_id === connectionId)
      // 版本号取自**当前列表**：调用方不必自己记，也就不会传一个更旧的版本上来。
      const expected = currentVersion ? Number(currentVersion.config_version) : undefined
      const updated = await run(connectionId, () =>
        updateModelConnection(connectionId, { ...body, expected_version: expected }),
      )
      if (updated) replaceOne(updated)
      return updated
    },
    [patch, replaceOne, run, state.connections],
  )

  const test = useCallback<ModelSettingsActions['test']>(
    async (connectionId) => {
      patch({ testingId: connectionId })
      const tested = await run(connectionId, () => testModelConnection(connectionId))
      patch({ testingId: null })
      if (tested) replaceOne(tested)
      return tested
    },
    [patch, replaceOne, run],
  )

  const selectDefault = useCallback<ModelSettingsActions['selectDefault']>(
    async (connectionId, justSaved) => {
      // 写选择要带两样：① 我看到的**当前默认**（别人刚改过就 409，而不是把它覆盖掉，§6.1）；
      // ② 目标连接**当时的版本**（这条连接在我点之前被改过也要拒绝）。
      // 两个都从当前列表取：调用方不必自己记版本，也就不会传一个更旧的上来。
      // 新增刚返回时 React 尚未完成下一次渲染：不能只从闭包里的旧列表找目标版本。
      const target = justSaved ?? state.connections.find((item) => item.connection_id === connectionId)
      const current = state.connections.find((item) => item.is_default)
      if (target === undefined) {
        patch({
          error: new ApiError({ status: 0, code: null, message: '连接已不在当前列表中，请刷新连接列表' }),
          conflict: null,
        })
        return null
      }
      const expectation = current
        ? { connection_id: current.connection_id, config_version: Number(current.config_version) }
        : { connection_id: null, config_version: null }
      const chosen = await run(connectionId, () =>
        selectDefaultModel(
          connectionId,
          expectation,
          Number(target.config_version),
        ),
      )
      if (chosen) {
        // 默认是全局唯一的：把其他条的 `is_default` 一起收敛，别留两个"默认"。
        if (!alive.current) return chosen
        setState((current) => ({
          ...current,
          connections: current.connections.map((item) => ({
            ...item,
            is_default: item.connection_id === connectionId,
          })),
        }))
      }
      return chosen
    },
    [patch, run, state.connections],
  )

  const retire = useCallback<ModelSettingsActions['retire']>(
    async (connectionId) => {
      const retired = await run(connectionId, () => retireModelConnection(connectionId))
      if (!retired) return false
      if (!alive.current) return true
      setState((current) => ({
        ...current,
        // 停用 = 从可用列表移除（列表接口也不返回停用的连接）。
        connections: current.connections.filter((item) => item.connection_id !== connectionId),
      }))
      return true
    },
    [run],
  )

  return { ...state, reload, save, test, selectDefault, retire }
}
