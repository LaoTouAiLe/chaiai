import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { WelcomePanel } from './WelcomePanel'

const empty = { title: '开始工作', description: '本地工作区', highlightsTitle: '可开始的任务', highlights: ['阅读项目', '修改文件', '验证结果'] }

describe('工作台任务起点', () => {
  it('快捷入口只交出草稿，不自行提交或执行任务', () => {
    const onUsePrompt = vi.fn()
    render(<WelcomePanel empty={empty} onUsePrompt={onUsePrompt} />)
    fireEvent.click(screen.getByRole('button', { name: /了解项目/ }))
    expect(onUsePrompt).toHaveBeenCalledExactlyOnceWith('请阅读当前工作区，解释项目结构、主要入口和运行方式。先阅读，不修改文件。')
  })
  it('静态展示没有动作时不提供可点击的假入口', () => {
    render(<WelcomePanel empty={empty} />)
    expect(screen.getByRole('button', { name: /了解项目/ })).toBeDisabled()
  })
})
