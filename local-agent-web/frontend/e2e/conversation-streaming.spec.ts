import { expect, test } from '@playwright/test'

const REPLY = '第一句回答已经开始。这里是持续生成的补充说明，会在后续块到来之前先展示给用户。第二句回答继续显示。这里继续提供更多可读内容，方便观察增量过程。第三句回答会在最后才出现。'

test('最终回答真正增量出现，并在完成与刷新后只留一份', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.getByRole('button', { name: '选择会话模式' }).click()
  const streamReady = page.waitForResponse((response) =>
    response.url().includes('/api/sessions/') && response.url().endsWith('/events') && response.status() === 200,
  )
  await page.getByRole('menu', { name: '会话模式' }).getByRole('menuitemradio', { name: /闲聊/ }).click()
  await streamReady
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
