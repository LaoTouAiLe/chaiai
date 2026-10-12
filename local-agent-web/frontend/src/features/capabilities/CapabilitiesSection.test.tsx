// 能力面板容器的取数行为（T56；§16.6）。
//
// 只钉两件事：DTO 被如实映射成面板能渲染的行；读失败时把服务端/传输层的说法**原样**摆出来，
// 而不是留一个空面板（那会让人以为"这台机器什么能力都没有"）。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({ getCapabilities: vi.fn(), setActiveProfile: vi.fn() }))

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getCapabilities: mock.getCapabilities,
  setActiveProfile: mock.setActiveProfile,
}))

import { CapabilitiesSection } from './CapabilitiesSection'

function wrapper({ children }: { children: ReactNode }) {
  // 关掉查询重试：产品默认会重试三次，用例不该把时间花在等退避上。
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  )
}

beforeEach(() => {
  mock.getCapabilities.mockReset()
  mock.setActiveProfile.mockReset()
})

describe('CapabilitiesSection', () => {
  it('maps the endpoint payload into rows the panel can render', async () => {
    mock.getCapabilities.mockResolvedValue({
      profile: 'researcher',
      capabilities: [
        {
          capability: 'web',
          label: '联网检索与抓取',
          available: false,
          reason: 'disabled',
          detail: '联网开关关着。',
          hint: '把 web_access.enabled 设为 true。',
        },
      ],
    })

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    expect(await screen.findByText('researcher')).toBeInTheDocument()
    expect(screen.getByText('开关已关闭')).toBeInTheDocument()
    expect(screen.getByText(/web_access\.enabled/)).toBeInTheDocument()
  })

  it('surfaces the read failure instead of leaving an empty panel', async () => {
    mock.getCapabilities.mockRejectedValue(new Error('后端不可达'))

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('后端不可达'))
  })

  it('switches the profile through the write endpoint and takes the server view as the truth', async () => {
    mock.getCapabilities.mockResolvedValue({
      profile: 'coder',
      default_profile: 'coder',
      profiles: ['coder', 'reviewer'],
      store_unavailable: false,
      capabilities: [
        { capability: 'files_write', label: '修改工作区文件', available: true, reason: 'available', detail: '当前可用。', hint: null },
      ],
    })
    // 服务端回的完整视图：档位变了，能力的可用性与"下一步"都跟着变——页面照单全收，不自己推算。
    mock.setActiveProfile.mockResolvedValue({
      profile: 'reviewer',
      default_profile: 'coder',
      profiles: ['coder', 'reviewer'],
      store_unavailable: false,
      capabilities: [
        {
          capability: 'files_write',
          label: '修改工作区文件',
          available: false,
          reason: 'profile',
          detail: '当前 Agent 档位没有包含这个能力。',
          hint: '把 profiles.active 切到包含它的档位。',
        },
      ],
    })

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    const select = await screen.findByLabelText('切换当前档位')
    fireEvent.change(select, { target: { value: 'reviewer' } })

    await waitFor(() => expect(mock.setActiveProfile).toHaveBeenCalledWith('reviewer'))
    // 换上的是服务端返回的那一份：能力从"可用"变成"当前档位没有"，且给出下一步。
    await waitFor(() => expect(screen.getByLabelText('切换当前档位')).toHaveValue('reviewer'))
    expect(screen.getByText('当前档位没有')).toBeInTheDocument()
    expect(screen.getByText(/profiles\.active/)).toBeInTheDocument()
  })

  it('keeps the previous view and shows the reason when the switch fails', async () => {
    mock.getCapabilities.mockResolvedValue({
      profile: 'coder',
      default_profile: 'coder',
      profiles: ['coder', 'reviewer'],
      store_unavailable: false,
      capabilities: [
        { capability: 'files_write', label: '修改工作区文件', available: true, reason: 'available', detail: '当前可用。', hint: null },
      ],
    })
    mock.setActiveProfile.mockRejectedValue(new Error('未知档位：nope'))

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    fireEvent.change(await screen.findByLabelText('切换当前档位'), { target: { value: 'reviewer' } })

    // 失败时**不动视图**：面板仍显示原来那一档与它的能力，错误单独提示（与 usePermissions 同一条做法）。
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('未知档位：nope'))
    expect(screen.getByLabelText('切换当前档位')).toHaveValue('coder')
    expect(screen.getByText('可用')).toBeInTheDocument()
  })
})
