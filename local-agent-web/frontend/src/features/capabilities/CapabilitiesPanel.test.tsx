// 能力面板的展示规则（T56；§16.6）。
//
// 这组用例钉的是**对外承诺**，不是实现细节：
//   1. 当前档位与每一项都要出现，且四项原因各自有自己的说法（不合并、不美化）；
//   2. 不可用**必须**给出下一步——只灰掉不解释会让人以为功能坏了；
//   3. 还没读到时不编造任何一项。

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { CapabilitiesView } from '../../fixtures/types'
import { CapabilitiesPanel } from './CapabilitiesPanel'

const VIEW: CapabilitiesView = {
  profile: 'coder',
  defaultProfile: 'coder',
  profiles: ['coder', 'researcher', 'reviewer'],
  storeUnavailable: false,
  capabilities: [
    {
      capability: 'plan',
      label: '任务计划',
      available: true,
      reason: 'available',
      detail: '当前可用。',
      hint: null,
    },
    {
      capability: 'web',
      label: '联网检索与抓取',
      available: false,
      reason: 'profile',
      detail: '当前 Agent 档位没有包含这个能力。',
      hint: '把 profiles.active 切到包含它的档位。',
    },
    {
      capability: 'git_write',
      label: '提交到 Git',
      available: false,
      reason: 'missing_dependency',
      detail: '这台机器上找不到 git 可执行文件。',
      hint: '安装 Git 后重启本应用。',
    },
  ],
}

describe('CapabilitiesPanel', () => {
  it('shows the current profile and each capability with its own reason wording', () => {
    render(<CapabilitiesPanel view={VIEW} />)

    expect(screen.getByText('coder')).toBeInTheDocument()
    expect(screen.getByText('任务计划')).toBeInTheDocument()
    expect(screen.getByText('可用')).toBeInTheDocument()
    // 档位问题与依赖问题**分开说**：合并成一句"不可用"就等于把用户的下一步藏起来
    expect(screen.getByText('当前档位没有')).toBeInTheDocument()
    expect(screen.getByText('本机缺少依赖')).toBeInTheDocument()
  })

  it('always offers the next step when something is unavailable', () => {
    render(<CapabilitiesPanel view={VIEW} />)

    expect(screen.getByText(/profiles\.active/)).toBeInTheDocument()
    expect(screen.getByText(/安装 Git 后重启本应用/)).toBeInTheDocument()
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

  it('lets the user switch the profile when the host wired it up', () => {
    const onSelectProfile = vi.fn()
    render(<CapabilitiesPanel view={VIEW} actions={{ onSelectProfile }} />)

    const select = screen.getByLabelText('切换当前档位')
    expect(select).toHaveValue('coder')
    // 选项来自**服务端给的可选集**（视图模型里那三个），页面不自己拼一份。
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
      'coder', 'researcher', 'reviewer',
    ])

    fireEvent.change(select, { target: { value: 'reviewer' } })

    expect(onSelectProfile).toHaveBeenCalledWith('reviewer')
  })

  it('keeps the profile as plain text when there is nothing to write to', () => {
    // ① 没接线（原型宿主）：纯文本——不给一个点了没反应的控件。
    const plain = render(<CapabilitiesPanel view={VIEW} />)
    expect(screen.queryByLabelText('切换当前档位')).not.toBeInTheDocument()
    expect(screen.getByText('coder')).toBeInTheDocument()
    plain.unmount()

    // ② 设置库不可用：同样纯文本，并如实说明档位来自配置文件（§16.6 的降级口径）。
    render(
      <CapabilitiesPanel
        view={{ ...VIEW, storeUnavailable: true }}
        actions={{ onSelectProfile: vi.fn() }}
      />,
    )
    expect(screen.queryByLabelText('切换当前档位')).not.toBeInTheDocument()
    expect(screen.getByText(/设置库不可用，档位来自配置文件/)).toBeInTheDocument()
  })

  it('disables the selector while a switch is in flight', () => {
    render(<CapabilitiesPanel view={VIEW} actions={{ onSelectProfile: vi.fn(), busy: true }} />)

    expect(screen.getByLabelText('切换当前档位')).toBeDisabled()
  })
})
