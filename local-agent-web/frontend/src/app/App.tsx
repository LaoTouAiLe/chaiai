import { useState } from 'react'

import { Composer } from '../components/Composer'
import { Sidebar } from '../components/Sidebar'
import { TopBar } from '../components/TopBar'
import { Conversation } from '../features/conversation/Conversation'
import { ModelSettingsDialog } from '../features/settings/ModelSettingsDialog'
import { SettingsDialog } from '../features/settings/SettingsDialog'
import { findScenario } from '../fixtures/scenarios'
import { sidebarWorkspaces } from '../fixtures/sidebar'
import { LiveApp } from './LiveApp'
import styles from './App.module.css'

export function App() {
  // 默认是**真实**会话壳（T30）。`?state=` 仍然可达，但它现在只服务一件事：
  // T26 的静态视觉基线（54 张截图按场景驱动）。产品入口不再经过 fixture。
  const scenarioId = readScenarioId()
  return scenarioId === null ? <LiveApp /> : <PrototypeHost scenarioId={scenarioId} />
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
  const live = readLiveFlag()
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
          activeTab="conversation"
        />
        <Conversation scenario={scenario} />
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
      </main>
    </div>
  )
}
