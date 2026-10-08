import { expect, test } from '@playwright/test'

const REPLY = '第一句回答已经开始。这里是持续生成的补充说明，会在后续块到来之前先展示给用户。第二句回答继续显示。这里继续提供更多可读内容，方便观察增量过程。第三句回答会在最后才出现。'

test('最终回答真正增量出现，并在完成与刷新后只留一份', async ({ page }) => {
  await page.goto('/')
  const created = page.waitForResponse((response) =>
    response.url().endsWith('/api/sessions') && response.request().method() === 'POST' && response.status() === 200,
  )
  // 增量来自**会话级** SSE：订阅在会话创建后建立，所以先等它，再切模式发消息。
  const subscribed = page.waitForResponse((response) =>
    response.url().includes('/api/sessions/') && response.url().endsWith('/events') && response.status() === 200,
  )
  await page.getByRole('button', { name: '新建会话' }).click()
  await created
  await subscribed
  // 切换到闲聊**不会**新建会话，也不再产生第二条 `/events` 订阅（2026-10-08 起模式切换用 PATCH
  // 改当前会话，见 `conversation-live.spec.ts` 的"在当前会话切换模式"用例），所以这里等 PATCH。
  const switched = page.waitForResponse((response) =>
    response.url().endsWith('/mode') && response.request().method() === 'PATCH' && response.status() === 200,
  )
  await page.getByRole('button', { name: '选择会话模式' }).click()
  await page.getByRole('menu', { name: '会话模式' }).getByRole('menuitemradio', { name: /闲聊/ }).click()
  await switched
  await expect(page.getByRole('button', { name: '选择会话模式' })).toContainText('闲聊')
  const input = page.getByPlaceholder('给智能体发消息')
  await input.fill('请回答')
  await page.getByRole('button', { name: '发送消息' }).click()

  // 假模型的每块延迟 0.7s：观察到中间态才能证明增量不是最终快照一次性替代。
  await page.waitForFunction(() => {
    const log = document.querySelector('[role="log"][aria-label="对话"]')
    return log?.textContent?.includes('第一句回答已经开始。') &&
      !log.textContent.includes('第三句回答会在最后才出现。')
  }, null, { timeout: 12_000 })
  await expect(page.getByRole('log', { name: '对话' })).toContainText(REPLY)
  await expect(page.getByText(REPLY)).toHaveCount(1)

  await page.reload()
  await expect(page.getByText(REPLY)).toHaveCount(1)
  await expect(page.getByText('请回答')).toHaveCount(1)
})
