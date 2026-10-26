// 数据位置（2026-10-26）：「设置 → 数据位置」里唯一可改的一项。
//
// 为什么不走偏好那条路：数据根**不能**存在设置库里——设置库自己就在它下面。写进去就等于
// 让一个东西定义自己的位置：改到新目录后新库是空的，那条偏好丢了，启动又回到旧位置。
// 所以它走一个**固定放在平台默认目录**的指针文件，与 `CODEX_HOME` / `HERMES_HOME` 同一性质，
// 只是可以在页面上改。
//
// 三条口径：
//   1. **改完必须重启**：路径在启动时解析，运行期改它只会造成"看起来生效了"；
//   2. **不自动迁移**：新目录是空的，旧数据留在原地——界面上明说，不假装搬了；
//   3. **环境变量优先**：那一档页面改不生效，所以输入框直接锁住并说明原因。

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  getStorageLocation,
  pickWorkspaceDirectory,
  resetStorageLocation,
  setStorageLocation,
} from '../../api/client'
import type { StorageLocationDto } from '../../api/client'
import styles from './PreferenceSection.module.css'

export const storageLocationQueryKey = ['storage-location'] as const

/**
 * 人类可读的字节数（2026-10-26）。
 *
 * 设置页只需要"这个盘还够不够"这个量级，不追求精确到字节——所以整数位在 10 以下时保留一位
 * 小数（`3.9 GB` 比 `3 GB` 有信息量），之上取整（`57 GB` 不必写成 `56.7 GB`）。
 */
function formatBytes(bytes: number): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'] as const
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  const shown = value < 10 && unit > 0 ? value.toFixed(1) : String(Math.round(value))
  return `${shown} ${units[unit]}`
}

/** 数据根现在由谁决定。`environment` 那档页面改不生效，必须让用户看见。 */
const SOURCE_LABEL: Record<string, string> = {
  environment: '环境变量 CHAIAI_HOME',
  settings: '这里的设置',
  config: 'config.yaml 的 data_root',
  default: '平台标准目录',
}

export function StorageLocationField() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: storageLocationQueryKey,
    queryFn: getStorageLocation,
    staleTime: Infinity,
    refetchOnMount: 'always',
  })
  const [draft, setDraft] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [restart, setRestart] = useState(false)
  const [picking, setPicking] = useState(false)

  /** 唤起本机原生目录选择器；取消（`path: null`）不改草稿也不报错。 */
  const choose = async () => {
    setPicking(true)
    setError(null)
    try {
      const picked = await pickWorkspaceDirectory()
      // `!= null` 同时排掉 null 与 undefined（契约里 path 可选）：两者都是"用户取消了"。
      if (picked.path != null && picked.path !== '') {
        setDraft(picked.path)
        setRestart(false)
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '打开目录选择器失败')
    } finally {
      setPicking(false)
    }
  }

  const view = query.data
  const apply = (next: StorageLocationDto) => {
    queryClient.setQueryData(storageLocationQueryKey, next)
    setDraft(null)
    setError(null)
    setRestart(next.restart_required)
  }
  const save = useMutation({
    mutationFn: (value: string) => setStorageLocation(value),
    onSuccess: apply,
    onError: (exc: Error) => setError(exc.message),
  })
  const reset = useMutation({
    mutationFn: () => resetStorageLocation(),
    onSuccess: apply,
    onError: (exc: Error) => setError(exc.message),
  })

  if (!view) {
    return <div role="status">{query.isError ? '读取数据位置失败。' : '正在读取数据位置…'}</div>
  }
  // 环境变量在解析链最前面：这一档改指针文件不会生效，所以输入框锁住而不是假装能改。
  const locked = view.source === 'environment'
  const value = draft ?? view.data_root

  return (
    <div className={styles.fields} aria-label="数据位置">
      <div className={styles.field}>
        <div className={styles.fieldText}>
          <strong>数据目录</strong>
          <small>
            所有会话、记忆、附件与日志都放在这里。当前来源：{SOURCE_LABEL[view.source] ?? view.source}。
            {view.used_bytes == null ? null : ` 当前占用 ${formatBytes(view.used_bytes)}。`}
            {view.disk_free_bytes == null ? null : `所在盘剩余 ${formatBytes(view.disk_free_bytes)}。`}
            改完**要重启后端**才生效；新目录是空的，旧数据不会自动搬过去。
            默认位置：{view.default_root}
          </small>
        </div>
        <span className={styles.pathRow}>
          <input
            type="text"
            aria-label="数据目录"
            value={value}
            disabled={locked}
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
              setRestart(false)
            }}
          />
          {/* 与「模型缓存目录」同一套入口（2026-10-26）：让人手打绝对路径是没道理的，
              拼错一个字符只会得到"目录不可写"，而他说不清自己错在哪。 */}
          <button
            type="button"
            className={styles.pathButton}
            aria-label="选择数据目录"
            disabled={locked || picking}
            onClick={() => void choose()}
          >
            {picking ? '选择中…' : '选择…'}
          </button>
        </span>
      </div>
      {/* 盘快满了才提示，且只在"默认位置"这一档（后端判的）：已经换过盘的人不该再被劝一次。 */}
      {view.low_disk ? (
        <p className={styles.error} role="alert">
          这个盘只剩
          {view.disk_free_bytes == null ? '很少空间' : ` ${formatBytes(view.disk_free_bytes)}`}
          ，会话与附件会持续增长。建议点「选择…」换到空间更大的盘，改完重启生效。
        </p>
      ) : null}
      {locked ? <p className={styles.note}>{view.note}</p> : null}
      {error ? <p role="alert" className={styles.error}>{error}</p> : null}
      {restart && !error ? <p role="status" className={styles.saved}>{view.note}</p> : null}
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.primary}
          disabled={locked || save.isPending || draft === null || draft === view.data_root}
          onClick={() => save.mutate(value)}
        >
          {save.isPending ? '正在保存…' : '保存'}
        </button>
        <button
          type="button"
          disabled={locked || reset.isPending || view.source === 'default'}
          onClick={() => reset.mutate()}
        >
          恢复默认位置
        </button>
      </div>
    </div>
  )
}
