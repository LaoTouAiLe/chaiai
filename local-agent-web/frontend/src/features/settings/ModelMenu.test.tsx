import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { modelMenuScenario } from '../../fixtures/scenarios'
import type { ModelMenuView } from '../../fixtures/types'
import { ModelMenu } from './ModelMenu'

function menuUnderTest(): ModelMenuView {
  if (modelMenuScenario.modelMenu === null) throw new Error('scenario has no model menu')
  return modelMenuScenario.modelMenu
}

describe('ModelMenu', () => {
  it('lists the available connections with their state', () => {
    render(<ModelMenu menu={menuUnderTest()} />)
    expect(screen.getByText('模型 A')).toBeInTheDocument()
    expect(screen.getByText('模型 B')).toBeInTheDocument()
    expect(screen.getByText('备用端点')).toBeInTheDocument()
    expect(screen.getByText('已验证')).toBeInTheDocument()
    expect(screen.getByText('不支持工具')).toBeInTheDocument()
  })

  it('marks exactly the chosen connection as selected', () => {
    render(<ModelMenu menu={menuUnderTest()} />)
    const selected = screen
      .getAllByRole('button')
      .filter((button) => button.getAttribute('aria-pressed') === 'true')
    expect(selected).toHaveLength(1)
    expect(selected[0]).toHaveTextContent('模型 B')
  })

  it('states that the switch applies to the next submit, not the running turn', () => {
    render(<ModelMenu menu={menuUnderTest()} />)
    expect(screen.getByText('已选择 模型 B · 从下一轮开始生效。')).toBeInTheDocument()
    expect(screen.getByText('正在运行的一轮：模型 A（本轮已固定）')).toBeInTheDocument()
  })
})
