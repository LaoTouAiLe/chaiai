// 「能力」的容器（T56；§16.1、§16.6）：取数 + 映射 + 改开关，展示交给 `CapabilitiesPanel`。
//
// 分工与 `ModelSettingsDialog` → `PermissionSection` 一致：容器认识 fetch 与视图模型映射，
// 展示组件不认识。数据源只有一处（`useCapabilities`），**不在这里做缓存或"记住上次结果"**——
// 服务端每次现算，页面保存一份旧结论正是这个面板要消灭的问题。
//
// 开关写入（§16.1，2026-10-13）：成功后用服务端返回的完整视图替换缓存——每一项的可用性与它的
// "下一步"都跟着变，页面不自己推算那份列表。写失败**不动视图**（与 `usePermissions` 同一条做法）。

import { asApiError, useCapabilities, useSetCapabilities } from '../../api/queries'
import { toCapabilitiesView } from '../../api/viewModels'
import { CapabilitiesPanel } from './CapabilitiesPanel'

export function CapabilitiesSection({ onClose, embedded = false }: { onClose?: () => void; embedded?: boolean }) {
  const query = useCapabilities()
  const setCapabilities = useSetCapabilities()
  // 读失败与写失败共用一处提示：用户要看到的是"刚才那次操作怎么了"。
  const failure = setCapabilities.error ?? query.error

  return (
    <CapabilitiesPanel
      view={query.data === undefined ? null : toCapabilitiesView(query.data)}
      errorText={failure === null ? null : asApiError(failure).message}
      actions={{
        // 只报告动过的那一项：服务端合并式写入，其余开关保持原样。
        onToggle: (capability, enabled) => setCapabilities.mutate({ [capability]: enabled }),
        busy: setCapabilities.isPending,
      }}
      onClose={onClose}
      embedded={embedded}
    />
  )
}
