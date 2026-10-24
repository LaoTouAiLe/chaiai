import { useState } from 'react'

import { Button } from '../../components/ui/Button'
import { SegmentedControl, type SegmentedOption } from '../../components/ui/SegmentedControl'
import { McpSection } from '../mcp/McpSection'
import { SkillsSection } from '../skills/SkillsSection'
import { CapabilitiesSection } from './CapabilitiesSection'
import styles from './CapabilitiesCenter.module.css'

type Tab = 'capabilities' | 'skills' | 'mcp'

const TABS: readonly SegmentedOption<Tab>[] = [
  { value: 'capabilities', label: '能力' },
  { value: 'skills', label: '技能' },
  { value: 'mcp', label: 'MCP 外部工具' },
]

/**
 * 「能力中心」整页（2026-10-22 V5，规范 §5.4）。
 *
 * 2026-10-19 曾把能力并进设置，理由是"同一件事不该有两个入口"；2026-10-22 用户改判为独立成页，
 * 依据是设置已有 14 个分区，能力开关却属于**高频日常操作**（今天能不能联网、能不能提交代码），
 * 埋在设置里每次都要多两步。因此三块从设置移出，只在这里提供。
 *
 * 三个分区组件本身没变，仍是设置里用的那三个（`embedded` 形态），只是换了外壳。
 */
export function CapabilitiesCenter({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<Tab>('capabilities')

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div className={styles.heading}>
          <span className={styles.eyebrow}>能力与工具</span>
          <h1 className={styles.title}>能力中心</h1>
          <p className={styles.subtitle}>开没开、能不能用、缺什么；开关改动对下一次提交生效，不用重启服务。</p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>返回会话</Button>
      </header>

      <div className={styles.tabBar}>
        <SegmentedControl options={TABS} value={tab} onChange={setTab} ariaLabel="能力中心分区" />
      </div>

      <div className={styles.body}>
        {tab === 'capabilities' ? <CapabilitiesSection embedded /> : null}
        {tab === 'skills' ? <SkillsSection /> : null}
        {tab === 'mcp' ? <McpSection /> : null}
      </div>
    </div>
  )
}
