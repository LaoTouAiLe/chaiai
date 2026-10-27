// 「关于」（2026-10-27）：版本与更新、卸载。
//
// 只钉四件用户会看到的事：当前版本与形态、检查更新的三种结果、卸载确认里的数据勾选、
// 以及"勾了也删不掉"时按钮与文案不撒谎。网络与卸载动作全部 mock —— 这组用例不发请求。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AppInfoDto } from '../../api/client'
import { AboutSection } from './AboutSection'

const mock = vi.hoisted(() => ({
  info: vi.fn(),
  check: vi.fn(),
  uninstall: vi.fn(),
}))

vi.mock('../../api/client', async (original) => ({
  ...(await original<typeof import('../../api/client')>()),
  getAppInfo: mock.info,
  checkForUpdate: mock.check,
  uninstallApp: mock.uninstall,
}))

function info(patch: Partial<AppInfoDto> = {}): AppInfoDto {
  return {
    version: '0.1.0',
    install_kind: 'installed',
    install_dir: 'C:/Users/me/AppData/Local/Programs/chaiai',
    data_root: 'C:/Users/me/AppData/Local/chaiai',
    data_root_source: 'default',
    build: {
      built_at: '2026-10-27T00:00:00+00:00',
      revision: 'abcdef1234567890',
      dirty: '0',
      python: '3.13.1',
    },
    uninstall_available: true,
    uninstall_note: null,
    can_remove_data: true,
    remove_data_note: null,
    ...patch,
  }
}

function host(view: AppInfoDto = info()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['app-info'], view)
  mock.info.mockResolvedValue(view)
  return render(
    <QueryClientProvider client={client}>
      <AboutSection />
    </QueryClientProvider>,
  )
}

describe('设置 · 关于（2026-10-27）', () => {
  it('shows the version, install kind and data root', () => {
    host()

    expect(screen.getByText('0.1.0')).toBeInTheDocument()
    expect(screen.getByText('安装版')).toBeInTheDocument()
    expect(screen.getByText(/AppData\/Local\/chaiai（来源：平台标准目录）/)).toBeInTheDocument()
  })

  it('reports a newer release with a link to the release page', async () => {
    mock.check.mockResolvedValue({
      status: 'update_available',
      current_version: '0.1.0',
      latest_version: '0.2.0',
      release_url: 'https://github.com/LaoTouAiLe/chaiai/releases/tag/v0.2.0',
      published_at: '2026-10-26T12:00:00Z',
      note: null,
    })
    host()

    fireEvent.click(screen.getByRole('button', { name: '检查更新' }))

    expect(await screen.findByText(/发现新版本 0\.2\.0/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '打开发布页' })).toHaveAttribute(
      'href',
      'https://github.com/LaoTouAiLe/chaiai/releases/tag/v0.2.0',
    )
  })

  it('treats a failed check as information, not an error', async () => {
    // 后端口径：查不了是 200 + unavailable，**不是**异常。页面要照常可用。
    mock.check.mockResolvedValue({
      status: 'unavailable',
      current_version: '0.1.0',
      release_url: 'https://github.com/LaoTouAiLe/chaiai/releases',
      note: '连不上 GitHub，检查更新失败。',
    })
    host()

    fireEvent.click(screen.getByRole('button', { name: '检查更新' }))

    expect(await screen.findByText(/连不上 GitHub/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.getByRole('button', { name: '检查更新' })).toBeEnabled()
  })

  it('asks before uninstalling and passes the explicit data choice', async () => {
    mock.uninstall.mockResolvedValue({
      scheduled: true,
      remove_data: true,
      data_dir: 'C:/Users/me/AppData/Local/chaiai',
      message: '卸载程序即将启动，本窗口会先退出；你的数据目录会一并删除。',
    })
    host()

    // 第一下只是展开确认：默认不删数据，不能一点就走。
    fireEvent.click(screen.getByRole('button', { name: /卸载/ }))
    expect(mock.uninstall).not.toHaveBeenCalled()

    fireEvent.click(screen.getByLabelText('同时删除数据目录'))
    fireEvent.click(screen.getByRole('button', { name: '确认卸载' }))

    expect(await screen.findByText(/本窗口会先退出/)).toBeInTheDocument()
    expect(mock.uninstall).toHaveBeenCalledWith(true)
  })

  it('disables the data checkbox when the root cannot be deleted safely', () => {
    host(
      info({
        can_remove_data: false,
        remove_data_note: '数据目录是用户主目录或它的上级，拒绝删除',
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: /卸载/ }))

    expect(screen.getByLabelText('同时删除数据目录')).toBeDisabled()
    expect(screen.getByText(/拒绝删除/)).toBeInTheDocument()
  })

  it('explains why a portable bundle has no uninstaller', () => {
    host(info({ install_kind: 'portable', uninstall_available: false, uninstall_note: '这是绿色版（解压即用），没有卸载程序。' }))

    expect(screen.getByRole('button', { name: /卸载/ })).toBeDisabled()
    // 「绿色版」在形态一行与说明里各出现一次：断言说明那句，避免与形态标签撞。
    expect(screen.getByText(/没有卸载程序/)).toBeInTheDocument()
  })
})
