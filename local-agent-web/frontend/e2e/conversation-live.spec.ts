import { expect, test } from '@playwright/test'

/**
 * 真后端端到端（T30 的验收路径）：空会话 → 新建 → 提交 → 回答 → 第二轮 → 刷新后一致。
 *
 * 为什么这条必须打真服务：`page.route` 打桩能验"页面会按我编的数据渲染"，验不了
 * "提交真的走通了幂等键、快照真的带着回答回来、刷新之后内容还一样"——而最后一条正是
 * T30 的停止条件之一（前端不得靠读本地 State 恢复）。
 *
 * 服务由 `playwright.live.config.ts` 拉起：`scripts/serve_fake.py`（注入确定性假模型，同源托管产物）。
 */
const REPLY = '这是假模型的固定回答。'

test('两轮真实对话，刷新后内容一致', async ({ page }) => {
  await page.goto('/')

  // 1) 空会话：没有任何会话时，页面说的是"还没有会话"，而不是拿 fixture 充数。
  await expect(page.getByText('还没有会话')).toBeVisible()

  // 2) 新建会话。
  await page.getByRole('button', { name: '新建会话' }).click()
  const input = page.getByPlaceholder('给智能体发消息')
  await expect(input).toBeVisible()

  // 这条服务**没开流式**（`serve_fake.py` 的默认）：页面必须明说"未流式"。
  // 判据来自服务端（快照的 `answer_streaming`，v0.8 T74/§19.4），不是页面自己推断。
  await expect(page.getByText('未流式')).toBeVisible()

  // 3) 第一轮：回答来自真后端（假模型的固定文本）。
  await input.fill('第一轮')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText(REPLY)).toHaveCount(1)
  // 提交成功后草稿清空：同一句话不该在成功后再发一遍。
  await expect(input).toHaveValue('')

  // 4) 第二轮：两轮都落在同一个会话里。
  await input.fill('第二轮')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText(REPLY)).toHaveCount(2)
  const log = page.getByRole('log', { name: '对话' })
  await expect(log.getByText('第一轮')).toBeVisible()
  await expect(log.getByText('第二轮')).toBeVisible()

  // 5) 刷新恢复：内容来自服务端快照，而不是浏览器里的本地状态。
  await page.reload()
  await expect(page.getByText(REPLY)).toHaveCount(2)
  await expect(page.getByRole('log', { name: '对话' }).getByText('第二轮')).toBeVisible()

  // 真会话菜单：选择写入服务端，下一轮才生效；之后可恢复跟随默认。
  await page.getByRole('button', { name: '选择会话模型' }).click()
  const menu = page.getByRole('region', { name: '模型选择' })
  await expect(menu).toBeVisible()
  await menu.getByRole('list', { name: '模型连接' }).getByRole('button').first().click()
  await expect(menu).toHaveCount(0)
  await page.getByRole('button', { name: '选择会话模型' }).click()
  await expect(menu.getByRole('button', { name: '恢复跟随默认模型' })).toBeVisible()
  await menu.getByRole('button', { name: '恢复跟随默认模型' }).click()
  await expect(menu).toHaveCount(0)
})

test('在当前会话切换模式，保留草稿并在刷新后恢复', async ({ page }) => {
  await page.goto('/')
  const firstCreated = page.waitForResponse((response) =>
    response.url().endsWith('/api/sessions') && response.request().method() === 'POST' && response.status() === 200)
  await page.getByRole('button', { name: '新建会话' }).click()
  const first = await (await firstCreated).json() as { session_id: string; mode: string }
  expect(first.mode).toBe('agent')
  const before = await (await page.request.get('/api/sessions')).json() as { session_id: string }[]
  await page.getByPlaceholder('给智能体发消息').fill('保留切换前草稿')

  await page.getByRole('button', { name: '选择会话模式' }).click()
  const modeMenu = page.getByRole('menu', { name: '会话模式' })
  await expect(modeMenu).toContainText('历史记录保留')
  const chatUpdated = page.waitForResponse((response) =>
    response.url().endsWith(`/api/sessions/${first.session_id}/mode`) && response.request().method() === 'PATCH' && response.status() === 200)
  await modeMenu.getByRole('menuitemradio', { name: /闲聊/ }).click()
  const chat = await (await chatUpdated).json() as { session_id: string; mode: string }
  expect(chat.session_id).toBe(first.session_id)
  expect(chat.mode).toBe('chat')
  await expect(page.getByRole('button', { name: '选择会话模式' })).toContainText('闲聊')
  await expect(page.getByPlaceholder('给智能体发消息')).toHaveValue('保留切换前草稿')

  await page.reload()
  await expect(page.getByRole('button', { name: '选择会话模式' })).toContainText('闲聊')
  const sessions = await (await page.request.get('/api/sessions')).json() as { session_id: string; mode: string }[]
  expect(sessions.find((item) => item.session_id === chat.session_id)?.mode).toBe('chat')
  expect(sessions).toHaveLength(before.length)
})
