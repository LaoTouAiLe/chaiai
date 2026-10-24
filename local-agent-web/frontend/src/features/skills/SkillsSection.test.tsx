import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mock = vi.hoisted(() => ({ getSkills: vi.fn(), setSkillEnabled: vi.fn() }))

vi.mock('../../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../api/client')>()),
  getSkills: mock.getSkills,
  setSkillEnabled: mock.setSkillEnabled,
}))

import { SkillsSection } from './SkillsSection'

function wrapper({ children }: { children: ReactNode }) {
  // 关掉查询重试：产品默认会重试三次，用例不该把时间花在等退避上。
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  )
}

const PAYLOAD = {
  enabled: true,
  store_unavailable: false,
  diagnostics: [],
  skills: [
    {
      name: 'browser-visual-check',
      description: '用本机浏览器截图并量尺寸自检',
      source: '个人级（.agents）',
      enabled: true,
      model_invocable: true,
      user_invocable: true,
      declared_capabilities: ['shell'],
    },
  ],
}

beforeEach(() => {
  mock.getSkills.mockReset()
  mock.setSkillEnabled.mockReset()
})

describe('SkillsSection', () => {
  it('maps the endpoint payload into rows the panel can render', async () => {
    mock.getSkills.mockResolvedValue(PAYLOAD)

    render(<SkillsSection />, { wrapper })

    expect(await screen.findByText('browser-visual-check')).toBeInTheDocument()
    expect(screen.getByLabelText('browser-visual-check开关')).toBeChecked()
  })

  it('surfaces the read failure instead of leaving an empty panel', async () => {
    mock.getSkills.mockRejectedValue(new Error('后端不可达'))

    render(<SkillsSection />, { wrapper })

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('后端不可达'))
  })

  it('writes the toggled skill and takes the server view as the truth', async () => {
    mock.getSkills.mockResolvedValue(PAYLOAD)
    // 服务端回的完整视图：这一条被禁用之后，页面照单全收，不自己推算。
    mock.setSkillEnabled.mockResolvedValue({
      ...PAYLOAD,
      skills: [{ ...PAYLOAD.skills[0], enabled: false }],
    })

    render(<SkillsSection />, { wrapper })

    fireEvent.click(await screen.findByLabelText('browser-visual-check开关'))

    // 只发动过的那一项，而且是**名字 + 目标值**（服务端按名字合并式写入）。
    await waitFor(() =>
      expect(mock.setSkillEnabled).toHaveBeenCalledWith('browser-visual-check', false),
    )
    await waitFor(() => expect(screen.getByLabelText('browser-visual-check开关')).not.toBeChecked())
    expect(screen.getByText('已禁用')).toBeInTheDocument()
  })

  it('keeps the current view when the write fails', async () => {
    mock.getSkills.mockResolvedValue(PAYLOAD)
    mock.setSkillEnabled.mockRejectedValue(new Error('没有发现名为 x 的技能'))

    render(<SkillsSection />, { wrapper })

    fireEvent.click(await screen.findByLabelText('browser-visual-check开关'))

    // 写失败**不动视图**：开关仍是原状态，错误单独提示（与 usePermissions 同一条做法）。
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('没有发现名为 x 的技能'))
    expect(screen.getByLabelText('browser-visual-check开关')).toBeChecked()
  })
})
