import { expect, test, type Locator, type Page } from '@playwright/test'

// 真服务验收独立能力开关。
// 能力开关位于侧栏的能力中心。
// 不假设"联网检索与抓取"有开关：它在当前服务端已是始终开启的基础能力。

async function openCapabilities(page: Page): Promise<Locator> {
  await page.getByRole('button', { name: '能力中心', exact: true }).click()
  const settings = page.getByRole('main')
  await expect(settings.getByRole('heading', { name: '能力中心' })).toBeVisible()
  return settings
}

test('能力开关保存后立即更新，关闭重开和刷新后仍一致', async ({ page }) => {
  await page.goto('/')

  const settings = await openCapabilities(page)
  const row = settings.getByRole('listitem').filter({ hasText: '按需加载技能' })
  const toggle = row.getByRole('checkbox', { name: '按需加载技能开关' })
  // 不预设初值：这项可能被之前的运行改过。先读出来，再翻到相反状态核对。
  const initiallyOn = await toggle.isChecked()
  await expect(row).toContainText(initiallyOn ? '可用' : '开关已关闭')

  // 开关由保存响应控制，不做乐观勾选；等待服务端返回后再核对状态。
  await toggle.click()
  await expect(toggle).toBeChecked({ checked: !initiallyOn })
  await expect(row).toContainText(initiallyOn ? '开关已关闭' : '可用')

  await settings.getByRole('button', { name: '返回会话' }).click()
  await expect(settings.getByRole('heading', { name: '能力中心' })).toHaveCount(0)

  await openCapabilities(page)
  await expect(toggle).toBeChecked({ checked: !initiallyOn })

  await page.reload()
  await openCapabilities(page)
  await expect(toggle).toBeChecked({ checked: !initiallyOn })

  // 收尾：恢复到进入时的状态，免得这份工作区数据被留成"改过"的样子。
  await toggle.click()
  await expect(toggle).toBeChecked({ checked: initiallyOn })
})
