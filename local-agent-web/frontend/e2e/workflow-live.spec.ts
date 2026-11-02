import { expect, test, type Page } from '@playwright/test'
import { randomUUID } from 'node:crypto'

async function createSession(page: import('@playwright/test').Page) {
  const response = page.waitForResponse((response) => response.url().endsWith('/api/sessions') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '新建会话' }).click()
  expect((await response).ok()).toBe(true)
  await expect(page.getByRole('log', { name: '对话' })).toContainText('在工作区里动手做事')
  return (await response).json() as Promise<{ session_id: string }>
}

// 页面已移除显式工作流表单；API 仍接受结构化请求，页面负责显示权威进度与审批。
async function submitWorkflow(page: Page, sessionId: string, text: string, workflow: object) {
  const result = await page.request.post(`/api/sessions/${sessionId}/messages`, {
    data: { text, client_request_id: randomUUID(), workflow },
  })
  expect(result.ok(), await result.text()).toBe(true)
  await page.reload()
}

for (const width of [1440, 1280, 640]) {
  test(`只读工作流提交与刷新恢复 ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/')
    const session = await createSession(page)
    await submitWorkflow(page, session.session_id, '核对取消与权限原文', {
      name: 'readonly_research', request: { pattern: 'docs/*.md', keywords: ['取消', '能力'] },
    })
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
  const session = await createSession(page)
  await submitWorkflow(page, session.session_id, '修复 clamp 边界并验证三个测试', {
    name: 'code_change', request: { source_path: 'bounds.py', test_paths: ['test_bounds.py'], expected_test_count: 3 },
  })
  await expect(page.getByLabel('工作流进度')).toContainText('等待审批')
  await page.reload()
  await page.getByRole('button', { name: '允许一次' }).click()
  await expect(page.getByLabel('工作流进度')).toContainText('代码修改与验证 · 已完成')
  await expect(page.getByLabel('工作流进度')).toContainText('模型 1')
  await expect(page.getByRole('log', { name: '对话' })).toContainText('3')
})

test('委派入口显式收窄目录并展示子任务证据', async ({ page }) => {
  await page.goto('/')
  const session = await createSession(page)
  await submitWorkflow(page, session.session_id, '仅在 docs 调查', {
    name: 'delegated_research', request: { tasks: [{ scope: 'docs', question: '核对取消与权限原文' }], concurrency: 1 },
  })
  await expect(page.getByLabel('工作流进度')).toContainText('委派只读调查 · 已完成')
  await expect(page.getByLabel('工作流进度')).toContainText('docs：已完成，证据 6')
  await page.reload()
  await expect(page.getByLabel('工作流进度')).toContainText('docs：已完成，证据 6')
})
