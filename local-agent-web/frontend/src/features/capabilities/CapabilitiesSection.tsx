// 「能力与档位」的容器（T56；§16.6）：取数 + 映射，展示交给 `CapabilitiesPanel`。
//
// 分工与 `ModelSettingsDialog` → `PermissionSection` 一致：容器认识 fetch 与视图模型映射，
// 展示组件不认识。数据源只有一处（`useCapabilities`），**不在这里做缓存或"记住上次结果"**——
// 服务端每次现算，页面保存一份旧结论正是这个面板要消灭的问题。

import { asApiError, useCapabilities } from '../../api/queries'
import { toCapabilitiesView } from '../../api/viewModels'
import { CapabilitiesPanel } from './CapabilitiesPanel'

export function CapabilitiesSection({ onClose }: { onClose: () => void }) {
  const query = useCapabilities()

  return (
    <CapabilitiesPanel
      view={query.data === undefined ? null : toCapabilitiesView(query.data)}
      errorText={query.error === null ? null : asApiError(query.error).message}
      onClose={onClose}
    />
  )
}
