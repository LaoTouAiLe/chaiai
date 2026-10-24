import { expect, test } from '@playwright/test'
import { mkdtemp, rmdir } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test('三档写入真实设置库并在刷新后恢复，旧沙箱开关与接口已删除', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '设置', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '设置', exact: true })
  await dialog.getByRole('tab', { name: '权限', exact: true }).click()
  const group = dialog.getByRole('radiogroup', { name: '权限档位' })
  await expect(group.getByRole('radio')).toHaveCount(3)
  await expect(group.getByRole('radio', { name: '工作区可写', exact: true })).toHaveAttribute('aria-checked', 'true')
  await expect(dialog.getByRole('switch', { name: '写侧沙箱' })).toHaveCount(0)

  for (const [label, mode] of [['只读', 'read-only'], ['完全放行', 'danger-full-access'], ['工作区可写', 'workspace-write']]) {
    const saved = page.waitForResponse(response => response.url().endsWith('/api/permissions') && response.request().method() === 'PUT')
    await group.getByRole('radio', { name: label, exact: true }).click()
    const response = await saved
    expect(response.ok()).toBeTruthy()
    const body = await response.json()
    expect(body.mode).toBe(mode)
    expect(Object.keys(body).some(key => key.startsWith('shell_sandbox'))).toBe(false)
    await expect(group.getByRole('radio', { name: label, exact: true })).toHaveAttribute('aria-checked', 'true')
    await page.reload()
    await page.getByRole('button', { name: '设置', exact: true }).click()
    await dialog.getByRole('tab', { name: '权限', exact: true }).click()
    await expect(group.getByRole('radio', { name: label, exact: true })).toHaveAttribute('aria-checked', 'true')
  }
  const retired = await page.request.put('/api/permissions/shell-sandbox', { data: { enabled: true } })
  // 静态托管的 GET 兜底会使未知 PUT 返回 405；两者都表示没有这条可执行接口。
  expect([404, 405]).toContain(retired.status())
  await expect(dialog).toContainText('受保护子树的写拒绝项建立失败时会阻止命令执行')
  await page.screenshot({ path: '../docs/ui-reference/permissions-review-live-20261015.png' })
})

test('外部目录只授予写权限，旧只读记录可移除，授权可持久化和撤销', async ({ page }) => {
  const writable = await mkdtemp(join(tmpdir(), 'chaiai-writable-'))
  const legacy = await mkdtemp(join(tmpdir(), 'chaiai-legacy-read-'))
  try {
    const seeded = await page.request.post('/api/permissions/grants', { data: { path: legacy, access: 'read' } })
    expect(seeded.ok()).toBeTruthy()
    await page.goto('/')
    const openPermissions = async () => {
      await page.getByRole('button', { name: '设置', exact: true }).click()
      await page.getByRole('tab', { name: '权限', exact: true }).click()
      return page.getByRole('dialog', { name: '设置', exact: true })
    }
    let dialog = await openPermissions()
    await expect(dialog).toContainText('旧只读记录（读取已默认允许）')
    await expect(dialog.getByRole('combobox')).toHaveCount(0)
    await dialog.getByRole('textbox', { name: '外部可写目录', exact: true }).fill(writable)
    const added = page.waitForResponse(response => response.url().endsWith('/api/permissions/grants') && response.request().method() === 'POST')
    await dialog.getByRole('button', { name: '添加可写目录', exact: true }).click()
    const addedResponse = await added
    expect(addedResponse.ok()).toBeTruthy()
    expect(addedResponse.request().postDataJSON()).toEqual({ path: writable, access: 'read_write' })
    await expect(dialog.getByRole('button', { name: /^撤销写入授权 / })).toHaveCount(1)
    await page.reload()
    dialog = await openPermissions()
    await expect(dialog.getByRole('button', { name: /^撤销写入授权 / })).toHaveCount(1)
    await dialog.getByRole('textbox', { name: '外部可写目录', exact: true }).scrollIntoViewIfNeeded()
    await page.screenshot({ path: '../docs/ui-reference/permissions-external-read-live-20261015.png' })
    for (const name of [/^移除只读记录 /, /^撤销写入授权 /]) {
      const removed = page.waitForResponse(response => response.url().includes('/api/permissions/grants?') && response.request().method() === 'DELETE')
      await dialog.getByRole('button', { name }).click()
      expect((await removed).ok()).toBeTruthy()
    }
    await expect(dialog).toContainText('本机外部读取无需授权')
    expect((await (await page.request.get('/api/permissions')).json()).grants).toEqual([])
  } finally {
    for (const path of [writable, legacy]) {
      await page.request.delete('/api/permissions/grants', { params: { path } })
      await rmdir(path) // 只删除 mkdtemp 创建的空目录，不递归操作其他路径。
    }
  }
})
