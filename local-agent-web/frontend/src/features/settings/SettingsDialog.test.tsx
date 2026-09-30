import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { settingsEmptyScenario, settingsModelsScenario } from '../../fixtures/scenarios'
import type { PermissionsView, SettingsView } from '../../fixtures/types'
import type { PermissionActions } from './PermissionSection'
import { SettingsDialog } from './SettingsDialog'

function settingsOf(scenario: typeof settingsModelsScenario): SettingsView {
  if (scenario.settings === null) throw new Error('scenario has no settings')
  return scenario.settings
}

function cardOf(name: string): HTMLElement {
  return screen.getByRole('article', { name: `连接 ${name}` })
}

function permissionsOf(overrides: Partial<PermissionsView> = {}): PermissionsView {
  return {
    mode: 'whitelist',
    defaultMode: 'whitelist',
    grants: [],
    storeUnavailable: false,
    ...overrides,
  }
}

function withPermissions(permissions: PermissionsView): SettingsView {
  return { ...settingsOf(settingsModelsScenario), permissions }
}

function permissionActions(overrides: Partial<PermissionActions> = {}): PermissionActions {
  return {
    onSelectMode: vi.fn(),
    onAddGrant: vi.fn(),
    onRevokeGrant: vi.fn(),
    onReload: vi.fn(),
    ...overrides,
  }
}

/** 打开「权限」分区；返回 user 供后续交互。 */
async function openPermissions(
  settings: SettingsView,
  actions: PermissionActions = permissionActions(),
): Promise<ReturnType<typeof userEvent.setup>> {
  const user = userEvent.setup()
  render(<SettingsDialog settings={settings} permissionActions={actions} />)
  await user.click(screen.getByRole('tab', { name: '权限' }))
  return user
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

  it('offers a section switcher that honours both sections', async () => {
    const user = userEvent.setup()
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)

    // T38 之前这里断言的是"没有分节切换器"，论据是"不摆做不到的分节"。现在两个分区都做得到，
    // 所以改成断言它**真的能切**：切过去要看得见权限分区的东西，而不是一个空壳标签。
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((tab) => tab.textContent)).toEqual(['模型连接', '权限'])
    expect(screen.getByRole('tab', { name: '模型连接' })).toHaveAttribute('aria-selected', 'true')
    // 默认停在模型连接：开设置绝大多数时候是为了"用哪条模型"。
    expect(screen.getAllByRole('article')).toHaveLength(6)

    await user.click(screen.getByRole('tab', { name: '权限' }))
    expect(screen.getByRole('tab', { name: '权限' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('radiogroup', { name: '工作区外路径' })).toBeInTheDocument()
    expect(screen.queryAllByRole('article')).toHaveLength(0)
  })
})

describe('SettingsDialog · 权限分区（T38）', () => {
  it('lays out all three presets and marks the one the server reports', async () => {
    await openPermissions(withPermissions(permissionsOf({ mode: 'whitelist' })))

    const group = screen.getByRole('radiogroup', { name: '工作区外路径' })
    expect(within(group).getAllByRole('radio')).toHaveLength(3)
    expect(within(group).getByRole('radio', { name: '手动审批' })).toHaveAttribute('aria-checked', 'false')
    expect(within(group).getByRole('radio', { name: '自定义' })).toHaveAttribute('aria-checked', 'true')
    expect(within(group).getByRole('radio', { name: '放行' })).toHaveAttribute('aria-checked', 'false')
  })

  it('hands the chosen preset back to the container instead of editing the view itself', async () => {
    const actions = permissionActions()
    const user = await openPermissions(withPermissions(permissionsOf({ mode: 'deny' })), actions)

    await user.click(screen.getByRole('radio', { name: '放行' }))
    expect(actions.onSelectMode).toHaveBeenCalledWith('allow')
  })

  it('shows the grant list only under the 自定义 preset', async () => {
    // 「放行」档不要求用户配任何东西：在"少打扰"档里让人一条条加目录，与那个档位的名字自相矛盾。
    for (const mode of ['deny', 'allow'] as const) {
      const { unmount } = render(<SettingsDialog settings={withPermissions(permissionsOf({ mode }))} />)
      const user = userEvent.setup()
      await user.click(screen.getByRole('tab', { name: '权限' }))

      expect(screen.queryByRole('button', { name: '添加授权' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /^撤销授权 / })).not.toBeInTheDocument()
      expect(screen.queryByLabelText('授权目录')).not.toBeInTheDocument()
      unmount()
    }
  })

  it('lists each grant with its level and a revoke entry', async () => {
    const actions = permissionActions()
    const user = await openPermissions(
      withPermissions(
        permissionsOf({
          mode: 'whitelist',
          grants: [
            { path: 'E:/design-assets', access: 'read' },
            { path: 'D:/team/shared', access: 'read_write' },
          ],
        }),
      ),
      actions,
    )

    const list = screen.getByRole('list')
    expect(within(list).getByText('E:/design-assets')).toBeInTheDocument()
    expect(within(list).getByText('只读')).toBeInTheDocument()
    expect(within(list).getByText('D:/team/shared')).toBeInTheDocument()
    expect(within(list).getByText('读写')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '撤销授权 E:/design-assets' }))
    expect(actions.onRevokeGrant).toHaveBeenCalledWith('E:/design-assets')
  })

  it('sends the typed path and level back, then clears the draft', async () => {
    const actions = permissionActions()
    const user = await openPermissions(
      withPermissions(permissionsOf({ mode: 'whitelist', grants: [] })),
      actions,
    )

    const input = screen.getByLabelText('授权目录')
    await user.type(input, 'D:\\shared\\data')
    await user.selectOptions(screen.getByLabelText('访问级别'), 'read_write')
    await user.click(screen.getByRole('button', { name: '添加授权' }))

    expect(actions.onAddGrant).toHaveBeenCalledWith('D:\\shared\\data', 'read_write')
    expect(input).toHaveValue('')
  })

  it('calls a blank list what it is — equivalent to 手动审批, not "no restriction"', async () => {
    await openPermissions(withPermissions(permissionsOf({ mode: 'whitelist', grants: [] })))

    expect(screen.getByText(/空白名单与「手动审批」等效/)).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('reports the factory default as a file fact, not as a product default', async () => {
    // 服务端报的当前档位是「手动审批」，出厂默认却是「自定义」：两件事必须分开说。
    await openPermissions(withPermissions(permissionsOf({ mode: 'deny', defaultMode: 'whitelist' })))

    expect(screen.getByText(/出厂默认（config.yaml）：自定义/)).toBeInTheDocument()
  })

  it('states the four hard constraints and the un-authorisable targets in every preset', async () => {
    for (const mode of ['deny', 'whitelist', 'allow'] as const) {
      const { unmount } = render(<SettingsDialog settings={withPermissions(permissionsOf({ mode }))} />)
      const user = userEvent.setup()
      await user.click(screen.getByRole('tab', { name: '权限' }))

      const text = screen.getByRole('tabpanel').textContent ?? ''
      expect(text).toContain('都必须人工批准')
      expect(text).toContain('不能被授权')
      unmount()
    }
  })

  it('says the change cannot be stored, instead of looking like it saved', async () => {
    const user = userEvent.setup()
    render(<SettingsDialog settings={settingsOf(settingsEmptyScenario)} />)
    await user.click(screen.getByRole('tab', { name: '权限' }))

    expect(screen.getByText(/没有可用的设置库/)).toBeInTheDocument()
  })

  it('shows no preset at all until the permissions have been read', async () => {
    await openPermissions({ ...settingsOf(settingsModelsScenario), permissions: null })

    expect(screen.getByText(/还没有读到权限设置/)).toBeInTheDocument()
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument()
  })

  it('surfaces a failed write with a way to read the permission settings again', async () => {
    const actions = permissionActions()
    const user = userEvent.setup()
    render(
      <SettingsDialog
        settings={withPermissions(permissionsOf({ mode: 'deny' }))}
        permissionError="设置库不可用：这次改动没有保存。"
        permissionActions={actions}
      />,
    )
    await user.click(screen.getByRole('tab', { name: '权限' }))

    expect(screen.getByRole('alert')).toHaveTextContent('这次改动没有保存')
    await user.click(screen.getByRole('button', { name: '重新读取权限设置' }))
    expect(actions.onReload).toHaveBeenCalled()
  })
})
