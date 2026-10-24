import { expect, test } from '@playwright/test'

async function createSession(page: import('@playwright/test').Page) {
  const response = page.waitForResponse((response) => response.url().endsWith('/api/sessions') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '新建会话' }).click()
  expect((await response).ok()).toBe(true)
  await expect(page.getByRole('log', { name: '对话' })).toContainText('在工作区里动手做事')
  await expect(page.getByLabel('执行方式')).toBeEnabled()
}

for (const width of [1440, 1280, 640]) {
  test(`只读工作流提交与刷新恢复 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/')
    await createSession(page)
    await page.getByLabel('执行方式').selectOption('readonly_research')
    await page.getByLabel('相对文件匹配（如 docs/*.md）').fill('docs/*.md')
    await page.getByLabel('关键词（每行一个，可留空）').fill('取消\n能力')
    await page.getByPlaceholder('给智能体发消息').fill('核对取消与权限原文')
    await page.getByRole('button', { name: '发送消息' }).click()
    const progress = page.getByLabel('工作流进度')
    await expect(progress).toContainText('只读调查 · 已完成')
    await expect(progress).toContainText('模型 0')
    await page.reload()
    await expect(progress).toContainText('只读调查 · 已完成')
    await expect(page.getByRole('log', { name: '对话' })).toContainText('取消不撤销已发生的副作用')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  })
}

test('代码工作流沿原审批入口恢复并核对真实测试结果', async ({ page }) => {
  await page.goto('/')
  await createSession(page)
  await page.getByLabel('执行方式').selectOption('code_change')
  await page.getByLabel('源码相对路径').fill('bounds.py')
  await page.getByLabel('测试相对路径（每行一个）').fill('test_bounds.py')
  await page.getByLabel('预期测试数').fill('3')
  await page.getByPlaceholder('给智能体发消息').fill('修复 clamp 边界并验证三个测试')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByLabel('工作流进度')).toContainText('等待审批')
  await page.reload()
  await page.getByRole('button', { name: '允许一次' }).click()
  await expect(page.getByLabel('工作流进度')).toContainText('代码修改与验证 · 已完成')
  await expect(page.getByLabel('工作流进度')).toContainText('模型 1')
  await expect(page.getByRole('log', { name: '对话' })).toContainText('3')
})

test('委派入口显式收窄目录并展示子任务证据', async ({ page }) => {
  await page.goto('/')
  await createSession(page)
  await page.getByLabel('执行方式').selectOption('delegated_research')
  await page.getByLabel('子任务 1 目录').fill('docs')
  await page.getByLabel('子任务 1 问题').fill('核对取消与权限原文')
  await page.getByPlaceholder('给智能体发消息').fill('仅在 docs 调查')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByLabel('工作流进度')).toContainText('委派只读调查 · 已完成')
  await expect(page.getByLabel('工作流进度')).toContainText('docs：已完成，证据 6')
  await page.reload()
  await expect(page.getByLabel('工作流进度')).toContainText('docs：已完成，证据 6')
})
