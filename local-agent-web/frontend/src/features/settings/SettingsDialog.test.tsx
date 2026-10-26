import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
    mode: 'workspace-write',
    defaultMode: 'workspace-write',
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
    onPickDirectory: vi.fn(),
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

/**
 * 展开一张连接卡的详情（2026-10-22 V4 起卡片默认收起）。
 * 端点、密钥、能力徽章、状态说明都在详情里，断言前必须先展开。
 */
function expandCard(name: string): HTMLElement {
  const card = cardOf(name)
  fireEvent.click(within(card).getByRole('button', { name: '详情' }))
  return card
}

describe('SettingsDialog', () => {
  it('locks page scrolling while open and restores it on close', () => {
    // 2026-10-26：设置里滚到边界后继续滚，背后的会话列表与输入框跟着动。
    // `.overlay` 本身不可滚动，`overscroll-behavior: contain` 挡不住它空白处的滚轮，
    // 所以打开期间直接锁 body——这条钉住"锁了，关掉要还回去"。
    const previous = document.body.style.overflow
    const { unmount } = render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)

    expect(document.body.style.overflow).toBe('hidden')

    unmount()
    expect(document.body.style.overflow).toBe(previous)
  })

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
    const untested = expandCard('模型 B')
    expect(within(untested).getByText('聊天 未知')).toBeInTheDocument()
    expect(within(untested).getByText('工具调用 未知')).toBeInTheDocument()
    expect(within(untested).getByText('流式输出 未知')).toBeInTheDocument()
    expect(within(untested).queryByText(/通过/)).not.toBeInTheDocument()
    // 备用端点 reports the one thing it actually failed at.
    expect(within(expandCard('备用端点')).getByText('工具调用 失败')).toBeInTheDocument()
  })

  it('reports key state without ever exposing a key value', () => {
    const { container } = render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    expect(within(expandCard('模型 A')).getByText('已配置（只能覆盖写入，不回读）')).toBeInTheDocument()
    expect(within(expandCard('未命名连接')).getByText('未配置')).toBeInTheDocument()
    expect(within(expandCard('本地端点')).getByText('仅本次进程内 · 重启后需重新录入')).toBeInTheDocument()

    const text = container.textContent ?? ''
    expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
    expect(text).not.toMatch(/[A-Za-z]:\\/)
  })

  it('never states an untested capability as usable', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    const detail = expandCard('模型 B').textContent ?? ''
    expect(detail).toContain('聊天能力未验证；可尝试普通聊天，工具能力尚未确认。')
    expect(detail).not.toMatch(/可用/)
  })

  it('marks plaintext transport on a non-loopback LAN endpoint', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    // 收起态就要能看到风险标记（不能靠点开才发现明文传输）
    expect(within(cardOf('本地端点')).getAllByText('明文传输').length).toBeGreaterThan(0)
    const risky = expandCard('本地端点')
    expect(within(risky).getByText(/请求内容在传输中不加密/)).toBeInTheDocument()
    // HTTPS endpoints carry no such mark.
    expect(within(expandCard('模型 A')).queryByText('明文传输')).not.toBeInTheDocument()
    expect(within(expandCard('旧供应商')).queryByText('明文传输')).not.toBeInTheDocument()
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
    expect(within(expandCard('模型 B')).getByText(/从下一轮请求开始生效/)).toBeInTheDocument()
  })

  it('explains an empty list and an unusable credential store', () => {
    render(<SettingsDialog settings={settingsOf(settingsEmptyScenario)} />)
    expect(screen.getByText('还没有模型连接')).toBeInTheDocument()
    expect(screen.queryAllByRole('article')).toHaveLength(0)
    // 文案与后端 `open_credential_store("memory")` 的降级说明一致（§6.8）：
    // 2026-10-13 起默认后端是本地设置库，这条只在"强制临时模式"时出现。
    expect(screen.getByText(/密钥只在本进程内有效，重启后需要重新录入/)).toBeInTheDocument()
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
    expect(screen.getByRole('radiogroup', { name: '权限档位' })).toBeInTheDocument()
    expect(screen.queryAllByRole('article')).toHaveLength(0)
  })
})

describe('SettingsDialog · 权限分区（T38）', () => {
  it('lays out all three presets and marks the one the server reports', async () => {
    await openPermissions(withPermissions(permissionsOf({ mode: 'workspace-write' })))

    const group = screen.getByRole('radiogroup', { name: '权限档位' })
    expect(within(group).getAllByRole('radio')).toHaveLength(3)
    expect(within(group).getByRole('radio', { name: '只读' })).toHaveAttribute('aria-checked', 'false')
    expect(within(group).getByRole('radio', { name: '工作区可写' })).toHaveAttribute('aria-checked', 'true')
    expect(within(group).getByRole('radio', { name: '常规免审' })).toHaveAttribute('aria-checked', 'false')
  })

  it('hands the chosen preset back to the container instead of editing the view itself', async () => {
    const actions = permissionActions()
    const user = await openPermissions(withPermissions(permissionsOf({ mode: 'read-only' })), actions)

    await user.click(screen.getByRole('radio', { name: '常规免审' }))
    expect(actions.onSelectMode).toHaveBeenCalledWith('danger-full-access')
  })

  it('shows the grant list in every preset — the tier never hides where writes can go', async () => {
    // 2026-10-15：工作区外的写**永远**只来自逐条授权，所以没有任何一档可以把这张列表藏起来
    // 只读档保留记录，但授权也不能使它可写。
    for (const mode of ['read-only', 'workspace-write', 'danger-full-access'] as const) {
      const { unmount } = render(<SettingsDialog settings={withPermissions(permissionsOf({ mode }))} />)
      const user = userEvent.setup()
      await user.click(screen.getByRole('tab', { name: '权限' }))

      expect(screen.getByRole('button', { name: '添加可写目录' })).toBeInTheDocument()
      expect(screen.getByLabelText('外部可写目录')).toBeInTheDocument()
      unmount()
    }
  })

  it('lists each grant with its level and a revoke entry', async () => {
    const actions = permissionActions()
    const user = await openPermissions(
      withPermissions(
        permissionsOf({
          mode: 'workspace-write',
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
    expect(within(list).getByText('旧只读记录（读取已默认允许）')).toBeInTheDocument()
    expect(within(list).getByText('D:/team/shared')).toBeInTheDocument()
    expect(within(list).getByText('可写授权')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '移除只读记录 E:/design-assets' }))
    expect(actions.onRevokeGrant).toHaveBeenCalledWith('E:/design-assets')
  })

  it('fills the writable directory from the picker instead of a typed absolute path', async () => {
    // 2026-10-26：这三处路径输入（模型缓存目录、数据位置、外部可写目录）都不该只靠手打。
    // 选择器由容器注入，`PermissionSection` 自己不认识 fetch。
    // `onPickDirectory` 是可选成员，直接写 `vi.fn()` 会被推断成普通函数（拿不到 `.mockResolvedValue`），
    // 所以显式构造一个异步桩再合并进去。
    const pick = vi.fn(async () => 'E:/design-assets')
    await openPermissions(
      withPermissions(permissionsOf({ mode: 'workspace-write', grants: [] })),
      { ...permissionActions(), onPickDirectory: pick },
    )

    await userEvent.setup().click(screen.getByRole('button', { name: '选择外部可写目录' }))

    await waitFor(() => expect(screen.getByLabelText('外部可写目录')).toHaveValue('E:/design-assets'))
  })

  it('adds a writable directory without asking for a redundant read level', async () => {
    const actions = permissionActions()
    const user = await openPermissions(
      withPermissions(permissionsOf({ mode: 'workspace-write', grants: [] })),
      actions,
    )

    const input = screen.getByLabelText('外部可写目录')
    await user.type(input, 'D:\\shared\\data')
    expect(screen.queryByLabelText('访问级别')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '添加可写目录' }))

    expect(actions.onAddGrant).toHaveBeenCalledWith('D:\\shared\\data', 'read_write')
    expect(input).toHaveValue('')
  })

  it('explains external reads remain available without writable grants', async () => {
    await openPermissions(withPermissions(permissionsOf({ mode: 'workspace-write', grants: [] })))

    expect(screen.getByText(/尚未添加外部可写目录/)).toBeInTheDocument()
    expect(screen.getByText(/本机外部读取无需授权/)).toBeInTheDocument()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('reports the factory default as a file fact, not as a product default', async () => {
    // 服务端报的当前档位是「只读」，出厂默认却是「工作区可写」：两件事必须分开说。
    await openPermissions(withPermissions(permissionsOf({ mode: 'read-only', defaultMode: 'workspace-write' })))

    expect(screen.getByText(/出厂默认（config.yaml）：工作区可写/)).toBeInTheDocument()
  })

  it('distinguishes rejected read-only writes from approvals and states protected targets in every preset', async () => {
    for (const mode of ['read-only', 'workspace-write', 'danger-full-access'] as const) {
      const { unmount } = render(<SettingsDialog settings={withPermissions(permissionsOf({ mode }))} />)
      const user = userEvent.setup()
      await user.click(screen.getByRole('tab', { name: '权限' }))

      const text = screen.getByRole('tabpanel').textContent ?? ''
      expect(text).toContain('「只读」档直接拒绝文件工具写入')
      expect(text).toContain('工作区内编辑、覆盖和删除默认免审批，外部写入需要目录授权')
      expect(text).toContain('破坏性 Shell 命令在任何档位下都须人工批准')
      expect(text).toContain('不能被授权')
      unmount()
    }
  })

  it('tells every preset what it does to Shell approvals, not only to paths', async () => {
    // 2026-10-14 起：三档的说明不能只讲路径——沙箱并入档位之后，"命令怎么跑"本就是档位的一部分。
    await openPermissions(withPermissions(permissionsOf({ mode: 'workspace-write' })))

    const group = screen.getByRole('radiogroup', { name: '权限档位' })
    const noteOf = (label: string) =>
      within(group).getByRole('radio', { name: label }).textContent ?? ''

    expect(noteOf('只读')).toMatch(/无可写 SID 的受限令牌/)
    expect(noteOf('工作区可写')).toMatch(/常规 Shell 命令免批准/)
    expect(noteOf('常规免审')).toMatch(/常规命令免批准/)
  })

  it('has no sandbox switch any more, and explains where the boundary comes from', async () => {
    // 2026-10-15 权限模型重构：写侧沙箱由档位派生，页面上不再有那个开关。
    await openPermissions(withPermissions(permissionsOf()))

    expect(screen.queryByRole('switch', { name: '写侧沙箱' })).not.toBeInTheDocument()
    expect(screen.getByText(/Windows 上各档位均通过受限令牌约束命令写入/)).toBeInTheDocument()
    // 边界仍必须说清：哪两档套沙箱、哪一档不套、其它平台为什么没有 OS 级边界。
    expect(screen.getByText(/其余档位可写工作区、专属临时目录和你授权的读写目录/)).toBeInTheDocument()
    expect(screen.getByText(/其它平台没有 OS 级写边界/)).toBeInTheDocument()
    expect(screen.getByText(/新建目录立即继承可写/)).toBeInTheDocument()
    expect(screen.getByText(/硬链接及并发改名仍有边界/)).toBeInTheDocument()
    // 2026-10-15（裁定 B2）：还要写清"有权改不了权限的目录会被跳过"以及两条修法——
    // 实证里用户只看到"写失败"，想不到原因，也想不到下一步。
    expect(screen.getByText(/沙箱会跳过它们/)).toBeInTheDocument()
    expect(screen.getByText(/请求提权（会弹审批、只对这一次生效）/)).toBeInTheDocument()
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
        settings={withPermissions(permissionsOf({ mode: 'read-only' }))}
        permissionError="设置库不可用：这次改动没有保存。"
        permissionActions={actions}
      />,
    )
    await user.click(screen.getByRole('tab', { name: '权限' }))

    expect(screen.getByRole('alert')).toHaveTextContent('这次改动没有保存')
    await user.click(screen.getByRole('button', { name: '重新读取权限设置' }))
    expect(actions.onReload).toHaveBeenCalled()
  })

  it('finds a setting across sections and lands on the owning tab', () => {
    render(
      <SettingsDialog
        settings={settingsOf(settingsModelsScenario)}
        extraSections={{
          retrieval: <p>检索占位</p>,
          web_access: <p>联网占位</p>,
          memory: <p>记忆占位</p>,
        }}
      />,
    )

    const box = screen.getByRole('searchbox', { name: '搜索设置项' })
    fireEvent.change(box, { target: { value: '域名' } })
    // 同名的「域名」相关项只应命中联网检索那条，且结果带所属分区。
    const hit = screen.getByRole('option', { name: /拒绝的域名/ })
    expect(within(hit).getByText('联网检索')).toBeInTheDocument()

    fireEvent.click(hit)

    expect(screen.getByRole('tab', { name: '联网检索' })).toHaveAttribute('aria-selected', 'true')
    expect(box).toHaveValue('')
  })

  it('opens the highlighted result with Enter', () => {
    render(
      <SettingsDialog
        settings={settingsOf(settingsModelsScenario)}
        extraSections={{
          retrieval: <p>检索占位</p>,
          memory: <p>记忆占位</p>,
        }}
      />,
    )

    const box = screen.getByRole('searchbox', { name: '搜索设置项' })
    fireEvent.change(box, { target: { value: '老化天数' } })
    fireEvent.keyDown(box, { key: 'Enter' })

    expect(screen.getByRole('tab', { name: '长期记忆' })).toHaveAttribute('aria-selected', 'true')
  })

  it('offers no search when only the hand-written sections are present', () => {
    render(<SettingsDialog settings={settingsOf(settingsModelsScenario)} />)
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  })
})
