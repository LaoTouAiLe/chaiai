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
  await expect(page.getByRole('alert')).toContainText('没有生成可展示的最终回答')

  await newSession('T30_EMPTY')
  await input.fill('T30_ERROR')
  await send.click()
  await expect(page.getByRole('alert')).toContainText('执行结果尚不能确认')
  // 只数对话里的那一条：提交失败后草稿按设计退回输入区（`LiveApp` 的 `onError`），
  // 输入区里的同一串文字不是"回答重复了一次"。
  await expect(log.getByText('T30_ERROR')).toHaveCount(1)
})
