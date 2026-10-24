// 能力面板的展示规则（T56；§16.1、§16.6）。
//
// 这组用例钉的是**对外承诺**，不是实现细节：
//   1. 每一项都要出现，且三种原因各自有自己的说法（不合并、不美化）；
//   2. 不可用**必须**给出下一步——只灰掉不解释会让人以为功能坏了；
//   3. 不可关闭的基础能力不给控件，而是说清"始终开启"；
//   4. 还没读到时不编造任何一项。

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { CapabilitiesView } from '../../fixtures/types'
import { CapabilitiesPanel } from './CapabilitiesPanel'

const VIEW: CapabilitiesView = {
  switches: {
    plan: true,
    files_read: true,
    files_write: false,
    web: false,
    git_write: false,
  },
  switchable: ['plan', 'files_write', 'web', 'git_write'],
  storeUnavailable: false,
  capabilities: [
    {
      capability: 'files_read',
      label: '读取工作区文件',
      available: true,
      reason: 'available',
      detail: '基础能力，始终开启（关掉它会让闲聊与只读调查都无解）。',
      hint: null,
      locked: true,
    },
    {
      capability: 'plan',
      label: '任务计划',
      available: true,
      reason: 'available',
      detail: '当前可用。',
      hint: null,
      locked: false,
    },
    {
      capability: 'web',
      label: '联网检索与抓取',
      available: false,
      reason: 'disabled',
      detail: '联网开关关着。',
      hint: '在「能力」面板里打开这一项（保存后下一轮生效，不用重启服务）。',
      locked: false,
    },
    {
      capability: 'git_write',
      label: '提交到 Git',
      available: false,
      reason: 'missing_dependency',
      detail: '这台机器上找不到 git 可执行文件。',
      hint: '安装 Git 后重启本应用。',
      locked: false,
    },
  ],
}

describe('CapabilitiesPanel', () => {
  it('shows each capability with its own reason wording', () => {
    render(<CapabilitiesPanel view={VIEW} />)

    expect(screen.getByText('任务计划')).toBeInTheDocument()
    // 两行可用（基础能力 + 任务计划），所以按"至少有一行"断言，不钉具体条数。
    expect(screen.getAllByText('可用').length).toBeGreaterThan(0)
    // 开关关闭与本机缺依赖**分开说**：合并成一句"不可用"就等于把用户的下一步藏起来
    expect(screen.getByText('开关已关闭')).toBeInTheDocument()
    expect(screen.getByText('本机缺少依赖')).toBeInTheDocument()
  })

  it('always offers the next step when something is unavailable', () => {
    render(<CapabilitiesPanel view={VIEW} />)

    expect(screen.getByText(/不用重启服务/)).toBeInTheDocument()
    expect(screen.getByText(/安装 Git 后重启本应用/)).toBeInTheDocument()
  })

  it('lists the base capabilities as always on instead of giving them a dead control', () => {
    render(<CapabilitiesPanel view={VIEW} />)

    expect(screen.getByText('始终开启')).toBeInTheDocument()
    // 不可关闭的能力**没有**开关——给一个点不动的控件比不给更糟。
    expect(screen.queryByLabelText('读取工作区文件开关')).not.toBeInTheDocument()
  })

  it('invents nothing before the first successful read', () => {
    render(<CapabilitiesPanel view={null} />)

    expect(screen.getByText(/还没有读到能力状态/)).toBeInTheDocument()
    expect(screen.queryByText('可用')).not.toBeInTheDocument()
  })

  it('shows the failure text verbatim instead of pretending everything is fine', () => {
    render(<CapabilitiesPanel view={null} errorText="请求失败（HTTP 500）" />)

    expect(screen.getByRole('alert')).toHaveTextContent('请求失败（HTTP 500）')
  })

  it('toggles one capability when the host wired it up', () => {
    const onToggle = vi.fn()
    render(<CapabilitiesPanel view={VIEW} actions={{ onToggle }} />)

    const web = screen.getByLabelText('联网检索与抓取开关')
    expect(web).not.toBeChecked()

    fireEvent.click(web)

    // 只报告动过的那一项；服务端合并式写入，其余开关保持原样。
    expect(onToggle).toHaveBeenCalledWith('web', true)
  })

  it('keeps the switches as plain text when there is nothing to write to', () => {
    // ① 没接线（原型宿主）：纯文本——不给一个点了没反应的控件。
    const plain = render(<CapabilitiesPanel view={VIEW} />)
    expect(screen.queryByLabelText('联网检索与抓取开关')).not.toBeInTheDocument()
    expect(screen.getByText('开关已关闭')).toBeInTheDocument()
    plain.unmount()

    // ② 设置库不可用：同样纯文本，并如实说明开关只来自配置文件（§16.1 的降级口径）。
    render(
      <CapabilitiesPanel
        view={{ ...VIEW, storeUnavailable: true }}
        actions={{ onToggle: vi.fn() }}
      />,
    )
    expect(screen.queryByLabelText('联网检索与抓取开关')).not.toBeInTheDocument()
    expect(screen.getByText(/开关来自配置文件时改完要重启服务/)).toBeInTheDocument()
  })

  it('disables the switches while a write is in flight', () => {
    render(<CapabilitiesPanel view={VIEW} actions={{ onToggle: vi.fn(), busy: true }} />)

    expect(screen.getByLabelText('联网检索与抓取开关')).toBeDisabled()
  })

  it('embeds into settings without its own dialog shell or close button', () => {
    // 2026-10-19：设置里的「能力」分区与顶栏面板共用同一份内容；内嵌时不能再叠一层
    // 遮罩对话框，也不能出现第二个"关闭"（关的是设置里的分区，不是整个面板）。
    render(<CapabilitiesPanel view={VIEW} embedded onClose={() => undefined} />)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByText('联网检索与抓取')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '关闭' })).not.toBeInTheDocument()
  })
})
