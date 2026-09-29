import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { settingsEmptyScenario, settingsModelsScenario } from '../../fixtures/scenarios'
import type { SettingsView } from '../../fixtures/types'
import { SettingsDialog } from './SettingsDialog'

function settingsOf(scenario: typeof settingsModelsScenario): SettingsView {
  if (scenario.settings === null) throw new Error('scenario has no settings')
  return scenario.settings
}

function cardOf(name: string): HTMLElement {
  return screen.getByRole('article', { name: `连接 ${name}` })
}

describe('SettingsDialog', () => {
  it('covers every connection state in one list', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    expect(screen.getAllByRole('article')).toHaveLength(6)
    expect(screen.getByText('已验证')).toBeInTheDocument()
    expect(screen.getAllByText('未验证')).toHaveLength(2)
    expect(screen.getByText('检测中')).toBeInTheDocument()
    expect(screen.getByText('检测失败')).toBeInTheDocument()
    expect(screen.getByText('能力不支持')).toBeInTheDocument()
  })

  it('marks exactly one default connection', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    expect(screen.getAllByText('默认')).toHaveLength(1)
    expect(within(cardOf('模型 A')).getByText('默认')).toBeInTheDocument()
  })

  it('never claims an untested capability passed', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    // 模型 B has never been checked: every capability reads "未知", none "通过".
    const untested = cardOf('模型 B')
    expect(within(untested).getByText('聊天 未知')).toBeInTheDocument()
    expect(within(untested).getByText('工具调用 未知')).toBeInTheDocument()
    expect(within(untested).getByText('流式输出 未知')).toBeInTheDocument()
    expect(within(untested).queryByText(/通过/)).not.toBeInTheDocument()
    // 备用端点 reports the one thing it actually failed at.
    expect(within(cardOf('备用端点')).getByText('工具调用 失败')).toBeInTheDocument()
  })

  it('reports key state without ever exposing a key value', () => {
    const { container } = render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    expect(within(cardOf('模型 A')).getByText('已配置（只能覆盖写入，不回读）')).toBeInTheDocument()
    expect(within(cardOf('未命名连接')).getByText('未配置')).toBeInTheDocument()
    expect(within(cardOf('本地端点')).getByText('仅本次进程内 · 重启后需重新录入')).toBeInTheDocument()

    const text = container.textContent ?? ''
    expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
    expect(text).not.toMatch(/[A-Za-z]:\\/)
  })

  it('never states an untested capability as usable', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    const detail = cardOf('模型 B').textContent ?? ''
    expect(detail).toContain('聊天能力未验证；可尝试普通聊天，工具能力尚未确认。')
    expect(detail).not.toMatch(/可用/)
  })

  it('marks plaintext transport on a non-loopback LAN endpoint', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    const risky = cardOf('本地端点')
    expect(within(risky).getByText('明文传输')).toBeInTheDocument()
    expect(within(risky).getByText(/请求内容在传输中不加密/)).toBeInTheDocument()
    // HTTPS endpoints carry no such mark.
    expect(within(cardOf('模型 A')).queryByText('明文传输')).not.toBeInTheDocument()
    expect(within(cardOf('旧供应商')).queryByText('明文传输')).not.toBeInTheDocument()
  })

  it('asks before deactivating and keeps history', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    const card = cardOf('未命名连接')
    expect(within(card).getByRole('button', { name: '确认删除' })).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: '取消' })).toBeInTheDocument()
    expect(within(card).getByText(/历史模型标识与用量记录保留/)).toBeInTheDocument()
    // Deactivation must not read as an immediate wipe.
    expect(within(card).getByText(/新请求不再使用该连接/)).toBeInTheDocument()
    expect(within(card).getByText(/正在运行的一轮继续使用它的固定版本与凭据/)).toBeInTheDocument()
    expect(card.textContent ?? '').not.toMatch(/清空/)
    // The plain single-click delete is replaced by the confirmation.
    expect(within(card).queryByRole('button', { name: '删除' })).not.toBeInTheDocument()
  })

  it('says when an edit only applies to the next request', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    expect(within(cardOf('模型 B')).getByText(/从下一轮请求开始生效/)).toBeInTheDocument()
  })

  it('explains an empty list and an unusable credential store', () => {
    render(<SettingsDialog settings={settingsOf(settingsEmptyScenario)} />)
    expect(screen.getByText('还没有模型连接')).toBeInTheDocument()
    expect(screen.queryAllByRole('article')).toHaveLength(0)
    expect(screen.getByText(/密钥只保存在本次进程内，重启后需要重新录入/)).toBeInTheDocument()
  })

  it('offers no section switcher it cannot honour', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    // v0.4 settings only manage connections; an inert nav item would promise more.
    expect(screen.queryByRole('tab')).not.toBeInTheDocument()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  })
})
