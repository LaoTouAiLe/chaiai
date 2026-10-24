import { expect, test } from '@playwright/test'
import type { components } from '../src/api/schema.gen'

test('真实压力触发动态压缩提示，完成后圆环变小且刷新保留完成提示', async ({ page }, info) => {
  await page.goto('/')
  const created = page.waitForResponse((r) => r.url().endsWith('/api/sessions') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '新建会话' }).click()
  const session = await (await created).json() as components['schemas']['SessionSummaryView']
  await page.getByRole('group', { name: '选择会话模式' }).getByRole('button', { name: '闲聊' }).click()
  await expect(page.getByRole('button', { name: '闲聊' })).toHaveAttribute('aria-pressed', 'true')
  const input = page.getByPlaceholder('给智能体发消息')
  await input.fill('COMPRESSION_SEED')
  const seeded = page.waitForResponse((r) => r.url().endsWith('/messages') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息' }).click()
  expect((await seeded).ok()).toBe(true)
  await expect(page.getByText('上下文已自动压缩', { exact: true })).toHaveCount(0)
  await input.fill('继续当前任务')
  const completed = page.waitForResponse((r) => r.url().endsWith('/messages') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(page.getByText('正在压缩…', { exact: true })).toBeVisible()
  await expect(page.getByText('上下文已自动压缩', { exact: true })).toHaveCount(0)
  const ring = page.getByRole('img', { name: /上下文已用/ })
  const before = Number((await ring.getAttribute('aria-label'))!.match(/上下文已用 (\d+)%/)![1])
  const live = await (await page.request.get(`/api/sessions/${session.session_id}`)).json() as components['schemas']['SessionSnapshot']
  expect(live.context?.compression?.state).toBe('running')
  expect(live.context?.window_tokens).toBe(12000)
  await page.screenshot({ path: info.outputPath('compressing.png') })
  expect((await completed).ok()).toBe(true)
  await expect(page.getByText('上下文已自动压缩', { exact: true })).toBeVisible()
  await expect(page.getByText('正在压缩…', { exact: true })).toHaveCount(0)
  const after = Number((await ring.getAttribute('aria-label'))!.match(/上下文已用 (\d+)%/)![1])
  expect(after).toBeLessThan(before)
  await page.screenshot({ path: info.outputPath('compressed.png') })
  await page.reload()
  await expect(page.getByText('上下文已自动压缩', { exact: true })).toBeVisible()
  const restored = await (await page.request.get(`/api/sessions/${session.session_id}`)).json() as components['schemas']['SessionSnapshot']
  expect(restored.context?.compression?.state).toBe('completed')
  expect(restored.context?.compression?.operation_id).toBe(live.context?.compression?.operation_id)
})
