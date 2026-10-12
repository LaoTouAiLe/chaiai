// 「任务中心」的容器（T62；§17.6）：取数 + 映射，展示交给 `TasksPanel`。
//
// 分工与 `CapabilitiesSection` → `CapabilitiesPanel` 一致：容器认识 fetch 与视图模型映射，
// 展示组件不认识。数据源只有一处（`useTasks`），**不在这里做缓存或"记住上次结果"**——
// 这一页要回答的是"任务现在到哪一步了"，保存一份旧结论正是它不该有的行为。

import { asApiError, useTasks } from '../../api/queries'
import { toTasksView } from '../../api/viewModels'
import { TasksPanel } from './TasksPanel'

export function TasksSection({
  workspaceId,
  onOpenSession,
  onClose,
}: {
  workspaceId: string | null
  onOpenSession: (sessionId: string) => void
  onClose: () => void
}) {
  const query = useTasks(workspaceId)

  return (
    <TasksPanel
      view={query.data === undefined ? null : toTasksView(query.data)}
      errorText={query.error === null ? null : asApiError(query.error).message}
      onOpenSession={onOpenSession}
      onClose={onClose}
    />
  )
}
