import { expect, test, type Page } from '@playwright/test'
import type { components } from '../src/api/schema.gen'

const REPLY = '第一句后台回答已经开始。第二句后台回答继续生成。第三句后台回答在最后完成。'
type Snapshot = components['schemas']['SessionSnapshot']
type MapView = components['schemas']['ConversationMapView']

async function newSession(page: Page) {
  const created = page.waitForResponse((r) => r.url().endsWith('/api/sessions') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '新建会话' }).click()
  const session = await (await created).json() as components['schemas']['SessionSummaryView']
  await expect(page.getByPlaceholder('给智能体发消息')).toBeVisible()
  return session
}

async function submit(page: Page, text: string) {
  await page.getByPlaceholder('给智能体发消息').fill(text)
  const response = page.waitForResponse((r) => r.url().endsWith('/messages') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息' }).click()
  const result = await (await response).json() as components['schemas']['CommandResultView']
  expect(result.status).toBe('running')
  expect(result.job_id).toBeTruthy()
  return result
}

test('后台任务切会话不串回答或草稿，刷新后恢复并能停止和继续', async ({ page }) => {
  await page.goto('/')
  const first = await newSession(page)
  await submit(page, '后台会话第一问')
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()
  await page.getByPlaceholder('给智能体发消息').fill('第一会话草稿')
  const second = await newSession(page)
  await page.getByPlaceholder('给智能体发消息').fill('第二会话草稿')
  await expect(page.getByRole('log', { name: '对话' })).not.toContainText(REPLY)
  await page.getByRole('navigation', { name: '会话' }).getByRole('button', { name: /^(运行中|空闲|已完成) 后台会话第一问 / }).click()
  await expect(page.getByPlaceholder('给智能体发消息')).toHaveValue('第一会话草稿')
  await expect(page.getByRole('log', { name: '对话' })).toContainText(REPLY)
  await page.reload()
  await expect(page.getByRole('log', { name: '对话' }).getByText(REPLY, { exact: true })).toHaveCount(1)
  const snapshot = await (await page.request.get(`/api/sessions/${first.session_id}`)).json() as Snapshot
  expect(snapshot.final_result?.state).toBe('completed')
  await submit(page, '后台停止这一问')
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()
  await page.getByRole('button', { name: '停止生成' }).click()
  await expect(page.getByText('已停止', { exact: true })).toBeVisible({ timeout: 3000 })
  await submit(page, '后台停止后的下一问')
  await expect(page.getByRole('log', { name: '对话' }).getByText(REPLY, { exact: true })).toHaveCount(2)
  // 第二会话没有收到第一会话的任务结果。
  const other = await (await page.request.get(`/api/sessions/${second.session_id}`)).json() as Snapshot
  expect(other.messages).toEqual([])
})

test('后台地图追问在抽屉内完成，关闭保留草稿，分支切换和刷新一致', async ({ page }) => {
  await page.goto('/')
  const session = await newSession(page)
  const log = page.getByRole('log', { name: '对话' })
  await submit(page, '地图后台起点')
  await expect(log).toContainText(REPLY)
  await submit(page, '地图后台原分支第二问')
  await expect(log.getByText(REPLY, { exact: true })).toHaveCount(2)
  const before = await (await page.request.get(`/api/sessions/${session.session_id}/conversation-map`)).json() as MapView
  const originalBranch = before.active_branch_id
  await page.getByRole('tab', { name: '会话地图' }).click()
  await page.getByRole('button', { name: /第1轮 · 已完成/ }).click()
  await page.getByRole('button', { name: '基于此轮继续提问' }).click()
  const drawer = page.getByRole('complementary', { name: '地图追问' })
  await drawer.getByPlaceholder('给智能体发消息').fill('后台地图分支追问')
  const response = page.waitForResponse((r) => r.url().endsWith('/messages') && r.request().method() === 'POST')
  await drawer.getByRole('button', { name: '发送消息' }).click()
  expect((await (await response).json() as components['schemas']['CommandResultView']).status).toBe('running')
  await expect(drawer.getByRole('button', { name: '停止生成' })).toBeVisible()
  await expect(drawer.getByRole('log', { name: '对话' })).toContainText('后台地图分支追问')
  await expect(drawer.getByRole('button', { name: '停止生成' })).toHaveCount(0)
  await expect(drawer.getByRole('log', { name: '对话' }).getByText(REPLY, { exact: true })).toHaveCount(2)
  await expect(page.getByRole('tab', { name: '会话地图' })).toHaveAttribute('aria-selected', 'true')
  const after = await (await page.request.get(`/api/sessions/${session.session_id}/conversation-map`)).json() as MapView
  expect(after.nodes).toHaveLength(3)
  expect(after.active_branch_id).not.toBe(originalBranch)
  expect(after.nodes.find((n) => n.question === '后台地图分支追问')?.parent_turn_id).toBe(before.nodes[0].turn_id)
  await expect(page.getByRole('combobox', { name: '当前分支' })).toHaveValue(after.active_branch_id)
  await drawer.getByPlaceholder('给智能体发消息').fill('地图关闭后草稿')
  // Escape 先关闭内层模型面板，不能把地图追问抽屉一起关掉。
  await drawer.getByRole('button', { name: '选择会话模型' }).click()
  await drawer.getByRole('button', { name: '关闭模型选择' }).focus()
  await page.keyboard.press('Escape')
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole('region', { name: '模型选择' })).toHaveCount(0)
  await expect(drawer.getByPlaceholder('给智能体发消息')).toHaveValue('地图关闭后草稿')
  await drawer.getByRole('button', { name: '关闭地图追问' }).click()
  await page.getByRole('tab', { name: '对话', exact: true }).click()
  await expect(page.getByPlaceholder('给智能体发消息')).toHaveValue('地图关闭后草稿')
  await page.getByRole('tab', { name: '会话地图' }).click()
  await page.getByRole('combobox', { name: '当前分支' }).selectOption(originalBranch)
  await page.getByRole('tab', { name: '对话', exact: true }).click()
  await expect(log).toContainText('地图后台原分支第二问')
  await expect(log).not.toContainText('后台地图分支追问')
  await page.reload()
  await expect(log).toContainText('地图后台原分支第二问')
  await expect(log).not.toContainText('后台地图分支追问')
})
