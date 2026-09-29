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
  await expect(page.getByText('第一轮')).toBeVisible()
  await expect(page.getByText('第二轮')).toBeVisible()

  // 5) 刷新恢复：内容来自服务端快照，而不是浏览器里的本地状态。
  await page.reload()
  await expect(page.getByText(REPLY)).toHaveCount(2)
  await expect(page.getByText('第二轮')).toBeVisible()

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
