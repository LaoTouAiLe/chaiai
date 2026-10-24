// 能力面板容器的取数与写入行为（T56；§16.1、§16.6）。
//
// 只钉三件事：DTO 被如实映射成面板能渲染的行；读失败时把服务端/传输层的说法**原样**摆出来，而不是留
// 一个空面板（那会让人以为"这台机器什么能力都没有"）；改开关成功后以**服务端返回的那一份**为准。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({ getCapabilities: vi.fn(), setCapabilities: vi.fn() }))

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getCapabilities: mock.getCapabilities,
  setCapabilities: mock.setCapabilities,
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
  mock.setCapabilities.mockReset()
})

describe('CapabilitiesSection', () => {
  it('maps the endpoint payload into rows the panel can render', async () => {
    mock.getCapabilities.mockResolvedValue({
      switches: { web: false },
      switchable: ['web'],
      capabilities: [
        {
          capability: 'web',
          label: '联网检索与抓取',
          available: false,
          reason: 'disabled',
          detail: '联网开关关着。',
          hint: '在「能力」面板里打开这一项（保存后下一轮生效，不用重启服务）。',
        },
      ],
    })

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    expect(await screen.findByText('开关已关闭')).toBeInTheDocument()
    expect(screen.getByText(/在「能力」面板里打开这一项/)).toBeInTheDocument()
    expect(screen.getByLabelText('联网检索与抓取开关')).not.toBeChecked()
  })

  it('surfaces the read failure instead of leaving an empty panel', async () => {
    mock.getCapabilities.mockRejectedValue(new Error('后端不可达'))

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('后端不可达'))
  })

  it('writes the toggled capability and takes the server view as the truth', async () => {
    mock.getCapabilities.mockResolvedValue({
      switches: { web: false },
      switchable: ['web'],
      store_unavailable: false,
      capabilities: [
        { capability: 'web', label: '联网检索与抓取', available: false, reason: 'disabled', detail: '联网开关关着。', hint: '在「能力」面板里打开这一项。' },
      ],
    })
    // 服务端回的完整视图：开关变了，能力的可用性与"下一步"都跟着变——页面照单全收，不自己推算。
    mock.setCapabilities.mockResolvedValue({
      switches: { web: true },
      switchable: ['web'],
      store_unavailable: false,
      capabilities: [
        { capability: 'web', label: '联网检索与抓取', available: true, reason: 'available', detail: '当前可用。', hint: null },
      ],
    })

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    fireEvent.click(await screen.findByLabelText('联网检索与抓取开关'))

    // 只发**动过的那一项**：服务端合并式写入，其余开关不受影响。
    await waitFor(() => expect(mock.setCapabilities).toHaveBeenCalledWith({ web: true }))
    // 换上的是服务端返回的那一份：能力从"开关已关闭"变成"可用"。
    await waitFor(() => expect(screen.getByLabelText('联网检索与抓取开关')).toBeChecked())
    expect(screen.getByText('可用')).toBeInTheDocument()
  })

  it('keeps the previous view and shows the reason when the switch fails', async () => {
    mock.getCapabilities.mockResolvedValue({
      switches: { web: false },
      switchable: ['web'],
      store_unavailable: false,
      capabilities: [
        { capability: 'web', label: '联网检索与抓取', available: false, reason: 'disabled', detail: '联网开关关着。', hint: '在「能力」面板里打开这一项。' },
      ],
    })
    mock.setCapabilities.mockRejectedValue(new Error('未登记的能力：nope'))

    render(<CapabilitiesSection onClose={() => undefined} />, { wrapper })

    fireEvent.click(await screen.findByLabelText('联网检索与抓取开关'))

    // 失败时**不动视图**：面板仍显示原来那一份状态，错误单独提示（与 usePermissions 同一条做法）。
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('未登记的能力：nope'))
    expect(screen.getByLabelText('联网检索与抓取开关')).not.toBeChecked()
    expect(screen.getByText('开关已关闭')).toBeInTheDocument()
  })
})
