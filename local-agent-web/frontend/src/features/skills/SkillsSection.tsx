// 「技能」的容器（技能管理页，2026-10-16；§16.5）：取数 + 映射 + 改开关，展示交给 `SkillsPanel`。
//
// 分工与 `CapabilitiesSection` → `CapabilitiesPanel` 一致：容器认识 fetch 与视图模型映射，展示组件
// 不认识。数据源只有一处（`useSkills`），**不在这里做缓存**——服务端每次现算（技能每轮重新发现），
// 页面保存一份旧结论正是"我明明放进去了怎么没有"的来源。
//
// 写入口径与 `useSetCapabilities` 相同：成功后用服务端返回的**完整视图**替换缓存，写失败**不动视图**
// （按钮解除忙态、错误单独提示）。

import { asApiError, useAddSkillRoot, useRemoveSkillRoot, useSetSkill, useSkills } from '../../api/queries'
import { toSkillsView } from '../../api/viewModels'
import { SkillsPanel } from './SkillsPanel'

export function SkillsSection() {
  const query = useSkills()
  const setSkill = useSetSkill()
  const addRoot = useAddSkillRoot()
  const removeRoot = useRemoveSkillRoot()
  // 读失败与写失败共用一处提示：用户要看到的是"刚才那次操作怎么了"。
  const failure = setSkill.error ?? addRoot.error ?? removeRoot.error ?? query.error

  return (
    <SkillsPanel
      view={query.data === undefined ? null : toSkillsView(query.data)}
      errorText={failure === null ? null : asApiError(failure).message}
      actions={{
        // 只报告动过的那一项：服务端按名字合并式写入，其余技能保持原样。
        onToggle: (name, enabled) => setSkill.mutate({ name, enabled }),
        // 发现根（2026-10-20）：保存即生效、作用于下一轮；服务端返回完整视图，页面直接换上。
        onAddRoot: (path) => addRoot.mutate(path),
        onRemoveRoot: (path) => removeRoot.mutate(path),
        busy: setSkill.isPending || addRoot.isPending || removeRoot.isPending,
      }}
    />
  )
}
