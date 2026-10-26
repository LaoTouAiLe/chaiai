import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { StorageLocationDto } from '../../api/client'
import { StorageLocationField, storageLocationQueryKey } from './StorageLocationField'

const mock = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), reset: vi.fn(), pick: vi.fn() }))
vi.mock('../../api/client', async original => ({
  ...await original<typeof import('../../api/client')>(),
  getStorageLocation: mock.get,
  setStorageLocation: mock.save,
  resetStorageLocation: mock.reset,
  pickWorkspaceDirectory: mock.pick,
}))

function view(patch: Partial<StorageLocationDto> = {}): StorageLocationDto {
  return {
    data_root: 'E:/data/chaiai',
    default_root: 'C:/Users/me/AppData/Local/chaiai',
    source: 'default',
    restart_required: false,
    note: null,
    // 有默认值（`bool = False`）→ 生成的类型里是必填，fixture 必须给基线。
    low_disk: false,
    ...patch,
  }
}

function host(data: StorageLocationDto = view()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(storageLocationQueryKey, data)
  mock.get.mockResolvedValue(data)
  return render(
    <QueryClientProvider client={client}><StorageLocationField /></QueryClientProvider>,
  )
}

describe('数据位置（2026-10-26）', () => {
  it('saves a new root and says it needs a restart', async () => {
    const current = view()
    mock.save.mockResolvedValue(view({
      data_root: 'E:/moved/chaiai', source: 'settings', restart_required: true,
      note: '已保存，重启后端后生效。新位置是空的：旧数据不会自动搬过去。',
    }))
    host(current)

    fireEvent.change(screen.getByLabelText('数据目录'), { target: { value: 'E:/moved/chaiai' } })
    fireEvent.click(screen.getByRole('button', { name: '保存' }))

    expect(await screen.findByRole('status')).toHaveTextContent('重启后端后生效')
    expect(mock.save).toHaveBeenCalledWith('E:/moved/chaiai')
  })

  it('fills the root from the native directory picker', async () => {
    // 2026-10-26：不再要求手打绝对路径。选中即进草稿，仍需点保存（写指针文件 + 重启生效）。
    mock.pick.mockReset()
    mock.pick.mockResolvedValue({ path: 'D:/chaiai-data' })
    host(view())

    fireEvent.click(screen.getByRole('button', { name: '选择数据目录' }))

    await waitFor(() => expect(screen.getByLabelText('数据目录')).toHaveValue('D:/chaiai-data'))
    expect(screen.getByRole('button', { name: '保存' })).toBeEnabled()
  })

  it('shows how much the root occupies and how much the disk has left', () => {
    // 2026-10-26：C 盘小的用户需要"为什么要改"的证据，而不只是一个输入框。
    host(view({ used_bytes: 4_180_000_000, disk_free_bytes: 60_900_000_000 }))

    expect(screen.getByText(/当前占用 3\.9 GB/)).toBeInTheDocument()
    expect(screen.getByText(/所在盘剩余 57 GB/)).toBeInTheDocument()
  })

  it('warns when the default location sits on a nearly full disk', () => {
    // 2026-10-26：C 盘小的用户第一眼就该看到"该换盘了"，而不是自己去资源管理器翻。
    host(view({ low_disk: true, disk_free_bytes: 2_000_000_000 }))

    expect(screen.getByRole('alert')).toHaveTextContent(/只剩 1\.9 GB/)
    expect(screen.getByRole('alert')).toHaveTextContent(/换到空间更大的盘/)
  })

  it('locks the field when an environment variable decides the root', () => {
    // 环境变量在解析链最前面：这一档页面改不生效，所以锁住输入框并说明原因，
    // 而不是让用户改完、重启、发现没变。
    host(view({ source: 'environment', note: '当前由环境变量 CHAIAI_HOME 决定：在页面上改不会生效，请改环境变量后重启。' }))

    expect(screen.getByLabelText('数据目录')).toBeDisabled()
    // "环境变量 CHAIAI_HOME"在两处出现（来源标签 + 说明段），所以按"至少出现"断言。
    expect(screen.getAllByText(/CHAIAI_HOME/).length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled()
  })

  it('offers 恢复默认位置 only when the root is not already the default', () => {
    const first = host(view({ source: 'settings', data_root: 'E:/moved' }))
    expect(screen.getByRole('button', { name: '恢复默认位置' })).toBeEnabled()
    first.unmount()
    // 已经是默认位置时不给这个按钮：点它等于"再写一遍默认值"，没有意义
    host(view({ source: 'default' }))
    expect(screen.getByRole('button', { name: '恢复默认位置' })).toBeDisabled()
  })
})
