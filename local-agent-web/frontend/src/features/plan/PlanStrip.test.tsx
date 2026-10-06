import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PlanView } from '../../fixtures/types'
import { PlanStrip } from './PlanStrip'

function planWith(steps: PlanView['steps']): PlanView {
  return { goal: '我们需要这个工作区中有类似项目吗？', steps }
}

const STEPS: PlanView['steps'] = [
  {
    id: 's1',
    title: '确认相似判断的参照对象：读取当前工作区根目录与当前项目标识；若用户未指定参照项目，则以当前项目为参照，并记录用途、技术栈、依赖、目录结构等可比对特征',
    status: 'failed',
  },
  { id: 's2', title: '先获取完整目录清单', status: 'completed' },
  { id: 's3', title: '暂停相似性结论', status: 'running' },
]

describe('PlanStrip', () => {
  it('starts collapsed so the steps cannot occupy the space above the composer', () => {
    render(<PlanStrip plan={planWith(STEPS)} />)

    // 目标与进度仍可见——收起的是步骤明细，不是整个计划。
    expect(screen.getByText('我们需要这个工作区中有类似项目吗？')).toBeInTheDocument()
    expect(screen.getByText('1/3 已完成')).toBeInTheDocument()
    expect(screen.queryByText('先获取完整目录清单')).not.toBeInTheDocument()

    const toggle = screen.getByRole('button', { expanded: false })
    fireEvent.click(toggle)

    expect(screen.getByText('先获取完整目录清单')).toBeInTheDocument()
    expect(screen.getByRole('button', { expanded: true })).toBeInTheDocument()
  })

  it('collapses back on the second click', () => {
    render(<PlanStrip plan={planWith(STEPS)} />)
    const toggle = screen.getByRole('button', { expanded: false })

    fireEvent.click(toggle)
    fireEvent.click(screen.getByRole('button', { expanded: true }))

    expect(screen.queryByText('暂停相似性结论')).not.toBeInTheDocument()
  })

  it('reports every step status without letting the title push it out', () => {
    render(<PlanStrip plan={planWith(STEPS)} />)
    fireEvent.click(screen.getByRole('button', { expanded: false }))

    // 状态是这一行最不能丢的信息：长标题只允许自己省略（完整文案在 title 里）。
    expect(screen.getByText('失败')).toBeInTheDocument()
    expect(screen.getByText('已完成')).toBeInTheDocument()
    expect(screen.getByText('进行中')).toBeInTheDocument()
    const long = screen.getByText(/确认相似判断的参照对象/)
    expect(long).toHaveAttribute('title', expect.stringContaining('可比对特征'))
  })

  it('keeps the running step summarised in the collapsed header', () => {
    render(<PlanStrip plan={planWith(STEPS)} />)

    expect(screen.getByText('· 暂停相似性结论')).toBeInTheDocument()
  })
})
