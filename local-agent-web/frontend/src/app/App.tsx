import { useMemo, useState } from 'react'

import { Composer } from '../components/Composer'
import { Sidebar } from '../components/Sidebar'
import { TopBar } from '../components/TopBar'
import { CapabilitiesPanel } from '../features/capabilities/CapabilitiesPanel'
import { TasksPanel } from '../features/tasks/TasksPanel'
import { Conversation } from '../features/conversation/Conversation'
import { ModelSettingsDialog } from '../features/settings/ModelSettingsDialog'
import { SettingsDialog } from '../features/settings/SettingsDialog'
import { traceFromMessages } from '../features/trace/adapters'
import { TraceView } from '../features/trace/TraceView'
import { findScenario } from '../fixtures/scenarios'
import { sidebarWorkspaces } from '../fixtures/sidebar'
import { LiveApp } from './LiveApp'
import styles from './App.module.css'

export function App() {
  // 默认是**真实**会话壳（T30）。`?state=` 仍然可达，但它现在只服务一件事：
  // 静态视觉基线（按场景驱动，张数与场景清单见 `local-agent-web/README.md`）。
  // 产品入口不再经过 fixture。
  const scenarioId = readScenarioId()
  return scenarioId === null ? (
    <LiveApp />
  ) : (
    // 按场景 id 重建宿主：换场景时页签要跟着场景走（新场景不得沿用上一个场景选中的页签）。
    <PrototypeHost key={scenarioId} scenarioId={scenarioId} />
  )
}

/** `?state=diff|errors|shell` 选择渲染哪个 fixture 场景（视觉基线专用宿主）。 */
function readScenarioId(): string | null {
  if (typeof location === 'undefined') return null
  return new URLSearchParams(location.search).get('state')
}

/**
 * `?live=1`：渲染**真实**设置对话框（连后端）而不是 fixture 原型。
 *
 * 产品路径已经不需要它了（设置由侧栏「设置」按钮打开）；保留是因为
 * `e2e/settings-live.spec.ts` 用它把真实对话框摆进已经稳定的场景壳里。
 */
function readLiveFlag(): boolean {
  if (typeof location === 'undefined') return false
  return new URLSearchParams(location.search).get('live') === '1'
}

/**
 * 原型宿主：**只**为视觉基线存在，不再显示"原型"标识条——T30 起产品走 `LiveApp`，
 * 一条"未接 Graph"的横幅已经没有对象可提醒，留着只会在基线里制造噪声。
 */
function PrototypeHost({ scenarioId }: { scenarioId: string | null }) {
  const scenario = findScenario(scenarioId)
  const [liveOpen, setLiveOpen] = useState(true)
  // 「能力与档位」面板的展开态：默认展开，关闭按钮真的能关（基线里要照到它，但不假装它关不掉）。
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(true)
  // 页签是界面状态；`?state=` 只决定初始页（`trace` 场景从轨迹页开始）。
  const [tab, setTab] = useState<'conversation' | 'trace'>(scenario.tab ?? 'conversation')
  const live = readLiveFlag()
  // 轨迹与对话来自**同一份消息**：原型里也不另写一套轨迹数据，否则两个页签可能互相矛盾。
  const trace = useMemo(
    () => traceFromMessages(scenario.messages, scenario.stats?.calls ?? []),
    [scenario],
  )
  const workspaces = sidebarWorkspaces.map((workspace) => ({
    ...workspace,
    sessions: workspace.sessions.map((session) => {
      const selected = scenario.sessionId !== null && session.id === scenario.sessionId
      // The selected row describes the session currently on screen, so its run
      // state comes from the scenario rather than from the sidebar fixture: a
      // stale "等待审批" dot would claim a decision is still pending after the
      // request expired. Unselected rows keep their own fixture state.
      return { ...session, selected, status: selected ? scenario.session.status : session.status }
    }),
  }))

  return (
    <div className={styles.app} data-scenario={scenario.id}>
      <Sidebar workspaces={workspaces} collapsed={scenario.sidebarCollapsed ?? false} />
      <main className={styles.main}>
        <TopBar
          title={scenario.session.title}
          workspace={scenario.session.workspace}
          activeTab={tab}
          onSelectTab={setTab}
        />
        {tab === 'trace' ? <TraceView trace={trace} /> : <Conversation scenario={scenario} />}
        <Composer
          session={scenario.session}
          plan={scenario.plan}
          meter={scenario.context}
          takeover={scenario.takeover}
          modelMenu={scenario.modelMenu}
          state={scenario.composer}
        />
        {live && liveOpen ? (
          <ModelSettingsDialog onClose={() => setLiveOpen(false)} />
        ) : scenario.settings === null ? null : (
          <SettingsDialog settings={scenario.settings} />
        )}
        {/* 只有挂了视图模型的场景才渲染（T56 的 `capabilities`）：其它场景的 DOM 与截图逐字节不变。 */}
        {scenario.capabilities == null || !capabilitiesOpen
          ? null
          : <CapabilitiesPanel view={scenario.capabilities} onClose={() => setCapabilitiesOpen(false)} />}
        {/* 同上（T62 的 `tasks`）：只有挂了视图模型的场景才渲染，"打开会话"在原型里是惰性的。 */}
        {scenario.tasks == null
          ? null
          : <TasksPanel view={scenario.tasks} onOpenSession={() => undefined} />}
      </main>
    </div>
  )
}
