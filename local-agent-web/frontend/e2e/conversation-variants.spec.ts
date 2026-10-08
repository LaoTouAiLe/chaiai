import { expect, test } from '@playwright/test'

test('长回答可滚到末尾、空回答与模型异常有可见状态', async ({ page }) => {
  await page.goto('/')
  const input = page.getByPlaceholder('给智能体发消息')
  const send = page.getByRole('button', { name: '发送消息' })
  const log = page.getByRole('log', { name: '对话' })
  const newSession = async (previousPrompt?: string) => {
    const created = page.waitForResponse((response) => response.url().endsWith('/api/sessions') &&
      response.request().method() === 'POST' && response.status() === 200)
    await page.getByRole('button', { name: '新建会话' }).click()
    await created
    await expect(input).toBeEnabled()
    if (previousPrompt !== undefined) await expect(log).not.toContainText(previousPrompt)
  }

  await newSession()
  await input.fill('T30_LONG')
  await send.click()
  await expect(log).toContainText('这是长回答。'.repeat(1500))
  await expect.poll(() => log.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight))
    .toBeLessThan(80)

  await newSession('T30_LONG')
  await input.fill('T30_EMPTY')
  await send.click()
  // 空回答：页面必须解释"这一轮为什么没有可展示的回答"。文案来自 `LiveApp` 的 failedWithoutDetail
  // 分支（`LiveApp.tsx`），同一条由 `LiveApp.test.tsx` 钉住；这里只断言它出现，不复制整句。
  await expect(page.getByRole('alert')).toContainText('这轮在给出最终回答前就结束了')

  await newSession('T30_EMPTY')
  await input.fill('T30_ERROR')
  await send.click()
  await expect(log).toContainText('本轮模型调用失败，已结束本轮')
  // 只数对话里的那一条：提交失败后草稿按设计退回输入区（`LiveApp` 的 `onError`），
  // 输入区里的同一串文字不是"回答重复了一次"。
  await expect(log.getByText('T30_ERROR')).toHaveCount(1)
  await page.reload()
  await input.fill('异常后继续讨论')
  await expect(send).toBeEnabled()
  await send.click()
  await expect(log).toContainText('这是假模型的固定回答。')
})
