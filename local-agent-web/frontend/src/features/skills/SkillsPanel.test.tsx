import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { SkillEntryView, SkillsView } from '../../fixtures/types'
import { SkillsPanel } from './SkillsPanel'

function skillOf(overrides: Partial<SkillEntryView> = {}): SkillEntryView {
  return {
    name: 'browser-visual-check',
    description: '用本机浏览器截图并量尺寸自检视觉效果',
    source: '个人级（.agents）',
    enabled: true,
    modelInvocable: true,
    userInvocable: true,
    declaredCapabilities: ['shell', 'files_read'],
    ...overrides,
  }
}

function viewOf(overrides: Partial<SkillsView> = {}): SkillsView {
  return {
    enabled: true,
    storeUnavailable: false,
    skills: [skillOf()],
    roots: [],
    diagnostics: [],
    ...overrides,
  }
}

describe('SkillsPanel', () => {
  it('lists a skill with its source layer and never shows a directory path', () => {
    render(<SkillsPanel view={viewOf()} />)

    expect(screen.getByText('browser-visual-check')).toBeInTheDocument()
    expect(screen.getByText('个人级（.agents）')).toBeInTheDocument()
    // 技能常在用户主目录下：这一页只报"来自哪一层"，不报路径（§15.8 的字段门槛）。
    expect(screen.queryByText(/\\\\|\/Users\/|\/home\//)).not.toBeInTheDocument()
  })

  it('says the whole list is inert while the master switch is off', () => {
    // 总闸关着时整页仍可读（用户要先看清再决定），但必须明说"现在对模型完全不可见"。
    render(<SkillsPanel view={viewOf({ enabled: false })} />)

    expect(screen.getByText(/技能能力当前/)).toHaveTextContent('关闭')
    expect(screen.getByText(/对模型完全不可见/)).toBeInTheDocument()
  })

  it('shows the skipped entries instead of silently dropping them', () => {
    render(
      <SkillsPanel
        view={viewOf({ diagnostics: ['技能名不是 kebab-case…已拒绝：My_Skill（个人级）'] })}
      />,
    )

    expect(screen.getByText(/有 1 条技能被跳过/)).toBeInTheDocument()
    expect(screen.getByText(/My_Skill/)).toBeInTheDocument()
  })

  it('reports only the skill that was toggled', () => {
    const onToggle = vi.fn()
    render(
      <SkillsPanel
        view={viewOf({ skills: [skillOf(), skillOf({ name: 'other-skill', enabled: false })] })}
        actions={{ onToggle }}
      />,
    )

    fireEvent.click(screen.getByLabelText('other-skill开关'))

    expect(onToggle).toHaveBeenCalledTimes(1)
    expect(onToggle).toHaveBeenCalledWith('other-skill', true)
  })

  it('falls back to plain text when there is no store to write to', () => {
    // 没有设置库时改动存不下来：此时**不给点不动的控件**（与能力面板同一条口径）。
    render(<SkillsPanel view={viewOf({ storeUnavailable: true })} actions={{ onToggle: vi.fn() }} />)

    expect(screen.queryByLabelText('browser-visual-check开关')).not.toBeInTheDocument()
    expect(screen.getByText(/改动存不下来/)).toBeInTheDocument()
  })

  it('keeps the author declaration apart from the user switch', () => {
    // 作者写 `disable-model-invocation` 时，模型看不到它——这与"用户开着"不是一回事，必须分开说。
    render(<SkillsPanel view={viewOf({ skills: [skillOf({ modelInvocable: false })] })} />)

    expect(screen.getByText(/只允许用户手动调用/)).toBeInTheDocument()
    expect(screen.getByText(/与这里的开关无关/)).toBeInTheDocument()
  })

  it('surfaces the failure instead of leaving an empty panel', () => {
    render(<SkillsPanel view={null} errorText="后端不可达" />)

    expect(screen.getByRole('alert')).toHaveTextContent('后端不可达')
    expect(screen.getByText(/还没有读到技能清单/)).toBeInTheDocument()
  })
})
