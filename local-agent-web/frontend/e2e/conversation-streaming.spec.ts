import { expect, test } from '@playwright/test'

const REPLY = '第一句回答已经开始。这里是持续生成的补充说明，会在后续块到来之前先展示给用户。第二句回答继续显示。这里继续提供更多可读内容，方便观察增量过程。第三句回答会在最后才出现。'

test('命令运行时逐行显示输出，刷新后继续接收并保留完整结果', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建会话' }).click()
  await page.getByPlaceholder('给智能体发消息').fill('COMMAND_STREAM')
  await page.getByRole('button', { name: '发送消息' }).click()
  const log = page.getByRole('log', { name: '对话' })
  const output = log.locator('pre').filter({ hasText: 'step-1' }).first()
  await expect(output).toBeVisible()
  await expect(output).not.toContainText('step-5')
  await expect(page.getByRole('button', { name: '停止生成' })).toBeVisible()
  await expect(output).toContainText('step-2')
  await page.reload()
  await expect(output).toContainText('step-1')
  await expect(output).toContainText('step-3')
  await expect(log.getByText(REPLY, { exact: true })).toHaveCount(1)
  await log.getByRole('button', { name: /运行命令.*command_progress_demo/ }).click()
  await expect(output).toContainText('step-5')
  await expect(log.getByText('退出码 0', { exact: true })).toBeVisible()
})

test('慢流停止保留部分回答、取消终态和地图锚点，并能立即发送下一条', async ({ page }) => {
  await page.goto('/')
  const created = page.waitForResponse((r) => r.url().endsWith('/api/sessions') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '新建会话' }).click()
  const summary = await (await created).json() as { session_id: string }
  await page.getByRole('group', { name: '选择会话模式' }).getByRole('button', { name: '闲聊' }).click()
  await expect(page.getByRole('button', { name: '闲聊' })).toHaveAttribute('aria-pressed', 'true')
  const input = page.getByPlaceholder('给智能体发消息')
  await input.fill('请慢慢回答，准备停止')
  const submitted = page.waitForResponse((r) => r.url().endsWith('/messages') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '发送消息' }).click()
  const log = page.getByRole('log', { name: '对话' })
  await expect(log).toContainText('第一句回答已经开始。')
  await page.getByRole('button', { name: '停止生成' }).click()
  await expect(page.getByText('已停止', { exact: true })).toBeVisible({ timeout: 3000 })
  expect((await (await submitted).json() as { status: string }).status).toBe('cancelled')
  await expect(log).not.toContainText('第三句回答会在最后才出现。')
  const snapshot = await (await page.request.get(`/api/sessions/${summary.session_id}`)).json() as {
    final_result: { state: string }; turn_usages: { usage: { final_state: string; final_at: string | null } }[];
  }
  expect(snapshot.final_result.state).toBe('cancelled')
  expect(snapshot.turn_usages.at(-1)?.usage.final_state).toBe('cancelled')
  expect(snapshot.turn_usages.at(-1)?.usage.final_at).not.toBeNull()
  const map = await (await page.request.get(`/api/sessions/${summary.session_id}/conversation-map`)).json() as {
    nodes: { state: string; can_continue: boolean }[];
  }
  expect(map.nodes.at(-1)).toMatchObject({ state: 'cancelled', can_continue: true })
  await input.fill('停止后的下一问')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeEnabled()
  await page.getByRole('button', { name: '发送消息' }).click()
  await expect(log).toContainText(REPLY)
})

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
  // 这条服务**开了流式**（`serve_fake.py --streaming`）：页面不得再说"未流式"（v0.8 T74/§19.4）。
  // 与 `conversation-live.spec.ts` 里那条断言成对：那个字段是真的跟着服务端走，不是写死的文案。
  await expect(page.getByText('未流式')).toHaveCount(0)
  // 切换到闲聊**不会**新建会话，也不再产生第二条 `/events` 订阅（2026-10-08 起模式切换用 PATCH
  // 改当前会话，见 `conversation-live.spec.ts` 的"在当前会话切换模式"用例），所以这里等 PATCH。
  // 模式切换是两段常驻控件（2026-10-13 换掉下拉）：一次点击即切换，没有中间菜单。
  const chat = page.getByRole('group', { name: '选择会话模式' }).getByRole('button', { name: '闲聊' })
  // 真服务共用历史，新建会话可能继承闲聊模式；已选中时不会再发 PATCH。
  if (await chat.getAttribute('aria-pressed') !== 'true') {
    const switched = page.waitForResponse((response) =>
      response.url().endsWith('/mode') && response.request().method() === 'PATCH' && response.status() === 200,
    )
    await chat.click()
    await switched
  }
  await expect(page.getByRole('button', { name: '闲聊' })).toHaveAttribute('aria-pressed', 'true')
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
  // 断言限定在**对话区**：本轮起会话按首条提问自动命名，侧栏那一行也会出现同一句话
  // （行按钮的可访问名 + 行内标题），整页计数会数到 3。这里要钉的是"刷新后本轮在对话里
  // 只有一份"，不是"这句话在整页只出现一次"。
  await expect(page.getByRole('log', { name: '对话' }).getByText('请回答')).toHaveCount(1)
})
