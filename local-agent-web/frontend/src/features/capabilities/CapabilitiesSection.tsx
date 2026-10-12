// 「能力与档位」的容器（T56；§16.6）：取数 + 映射 + 切换档位，展示交给 `CapabilitiesPanel`。
//
// 分工与 `ModelSettingsDialog` → `PermissionSection` 一致：容器认识 fetch 与视图模型映射，
// 展示组件不认识。数据源只有一处（`useCapabilities`），**不在这里做缓存或"记住上次结果"**——
// 服务端每次现算，页面保存一份旧结论正是这个面板要消灭的问题。
//
// 档位切换（§16.6，2026-10-12）：写成功后用服务端返回的完整视图替换缓存——能力和它的"下一步"
// 都跟着变，页面不自己推算那份列表。写失败**不动视图**（与 `usePermissions` 同一条做法）。

import { asApiError, useCapabilities, useSetActiveProfile } from '../../api/queries'
import { toCapabilitiesView } from '../../api/viewModels'
import { CapabilitiesPanel } from './CapabilitiesPanel'

export function CapabilitiesSection({ onClose }: { onClose: () => void }) {
  const query = useCapabilities()
  const setProfile = useSetActiveProfile()
  // 读失败与写失败共用一处提示：用户要看到的是"刚才那次操作怎么了"。
  const failure = setProfile.error ?? query.error

  return (
    <CapabilitiesPanel
      view={query.data === undefined ? null : toCapabilitiesView(query.data)}
      errorText={failure === null ? null : asApiError(failure).message}
      actions={{ onSelectProfile: (profile) => setProfile.mutate(profile), busy: setProfile.isPending }}
      onClose={onClose}
    />
  )
}
