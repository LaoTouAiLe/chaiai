// 设置对话框的**容器**（T29A）：把传输/状态层接到纯展示的 `SettingsDialog` 上。
//
// 分工：展示组件不认识 fetch 与视图模型映射；容器不认识样式与布局。页面本地态（确认删除、
// 正在探测、刚保存待下一轮生效、表单开关）都留在容器里，通过视图模型注入，不污染 DTO。

import { useCallback, useMemo, useState } from 'react'

import type { ModelConnectionDto, ModelConnectionInput } from '../../api/client'
import { toSettingsView } from '../../api/viewModels'
import { ConnectionForm } from './ConnectionForm'
import { SettingsDialog } from './SettingsDialog'
import { useModelSettings } from './useModelSettings'
import styles from './SettingsDialog.module.css'

export function ModelSettingsDialog({ onClose }: { onClose: () => void }) {
  const settings = useModelSettings()
  const [formOpen, setFormOpen] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [confirmId, setConfirmId] = useState<string | null>(null)
  /** 创建已成功但设默认失败：保留这条记录，重试时只操作选择，绝不再 POST 新连接。 */
  const [partialSaveId, setPartialSaveId] = useState<string | null>(null)
  /** 刚保存过的连接：卡片提示"从下一轮请求开始生效"（正在运行的一轮已固定自己的版本）。 */
  const [pendingId, setPendingId] = useState<string | null>(null)

  const editing = useMemo(
    () => settings.connections.find((item) => item.connection_id === editingId) ?? null,
    [editingId, settings.connections],
  )

  const view = useMemo(
    () =>
      toSettingsView(
        settings.connections,
        settings.health,
        (dto: ModelConnectionDto) => ({
          confirmDelete: dto.connection_id === confirmId,
          pendingApply: dto.connection_id === pendingId,
          testing: dto.connection_id === settings.testingId,
        }),
      ),
    [confirmId, pendingId, settings.connections, settings.health, settings.testingId],
  )

  const closeForm = useCallback(() => {
    setFormOpen(false)
    setEditingId(null)
  }, [])

  const submit = useCallback(
    async (input: ModelConnectionInput, apiKey: string | null) => {
      const existing = editingId
      const saved = await settings.save(input, {
        connectionId: existing ?? undefined,
        apiKey,
      })
      if (saved === null) return // 失败时表单留在原地：错误与提示都挂在它下面
      if (existing === null) {
        // 新连接直接成为默认：§15.9 要求"完成首个有效连接后无需重启即可对话"。
        const selected = await settings.selectDefault(saved.connection_id, saved)
        if (selected === null) {
          // 第一段 POST 已成功。关闭"新增"表单，否则用户再按保存会重复创建；
          // 卡片与提示继续显示，刷新后可只重试"设为默认"。
          setPartialSaveId(saved.connection_id)
          closeForm()
          return
        }
      }
      setPartialSaveId(null)
      setPendingId(saved.connection_id)
      closeForm()
    },
    [closeForm, editingId, settings],
  )

  const form = formOpen ? (
    <ConnectionForm
      initial={editing}
      // 新增时没有 id，忙态在 `creating` 上：两者合起来才是"表单正在提交"。
      busy={settings.busyId !== null || settings.creating}
      error={settings.conflict ?? settings.error}
      onSubmit={(input, apiKey) => void submit(input, apiKey)}
      onCancel={closeForm}
    />
  ) : partialSaveId !== null || settings.conflict !== null || settings.error !== null ? (
    // 只重试选择，不再重做已经成功的创建；其他冲突同样要求先刷新。
    <p className={styles.formError} role="alert">
      {partialSaveId === null ? null : '连接已保存，但尚未设为默认。请刷新后在该连接卡片点击「设为默认」。'}
      {settings.conflict?.message ?? settings.error?.message}
      <button type="button" className={styles.ghost} onClick={() => void settings.reload()}>
        刷新连接列表
      </button>
    </p>
  ) : null

  return (
    <SettingsDialog
      settings={view}
      form={form}
      actions={{
        busyId: settings.busyId,
        creating: settings.creating,
        onAdd: () => {
          setEditingId(null)
          setFormOpen(true)
        },
        // 编辑入口：打开表单并**带上这条连接的当前值**（密钥不在其中——契约不给，也就无从回显）。
        onEdit: (id) => {
          setEditingId(id)
          setFormOpen(true)
        },
        onTest: (id) => void settings.test(id),
        onSelectDefault: (id) => {
          void settings.selectDefault(id).then((selected) => {
            if (selected !== null) setPartialSaveId(null)
          })
        },
        onRequestDelete: (id) => setConfirmId(id),
        onCancelDelete: () => setConfirmId(null),
        onConfirmDelete: (id) => {
          setConfirmId(null)
          void settings.retire(id).then((retired) => {
            if (retired && id === partialSaveId) setPartialSaveId(null)
          })
        },
        onClose,
      }}
    />
  )
}
