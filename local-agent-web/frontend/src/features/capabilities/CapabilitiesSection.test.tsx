// 能力面板容器的取数行为（T56；§16.6）。
//
// 只钉两件事：DTO 被如实映射成面板能渲染的行；读失败时把服务端/传输层的说法**原样**摆出来，
// 而不是留一个空面板（那会让人以为"这台机器什么能力都没有"）。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({ getCapabilities: vi.fn() }))

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getCapabilities: mock.getCapabilities,
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
})
