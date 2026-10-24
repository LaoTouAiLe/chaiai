import { test, expect } from '@playwright/test'

test('真实受控终端：审批、输入、刷新重连与结束', async ({ page }, testInfo) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto('/')
  const beta = page.getByRole('button', { name: /知道了|开始使用/ })
  if (await beta.count()) await beta.first().click()
  await page.getByRole('button', { name: '新建会话', exact: true }).click()
  await page.getByRole('button', { name: '工作面板', exact: true }).click()
  await page.getByRole('tab', { name: '终端', exact: true }).click()
  const pane = page.getByRole('region', { name: '受控终端' })
  await pane.getByRole('button', { name: '新建', exact: true }).click()
  await expect(pane.getByText('批准启动终端')).toBeVisible()
  await expect(pane.locator('.xterm')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('approval.png') })
  const approved = page.waitForResponse(response => response.url().endsWith('/decision') && response.ok())
  await pane.getByRole('button', { name: '批准并启动' }).click()
  const terminal = await (await approved).json() as { terminal_id: string; workspace_id: string; status: string }
  expect(terminal.status).toBe('running')
  await expect(pane.getByRole('status')).toHaveText('已连接')
  const input = pane.locator('textarea.xterm-helper-textarea')
  await input.focus()
  await page.keyboard.type('echo LIVE_TERMINAL>terminal-live.txt')
  await page.keyboard.press('Enter')
  // Assert a filesystem side effect from the real process, not echoed input text.
  await expect.poll(async () => {
    const response = await page.request.get(`/api/data-center/artifacts/preview?path=terminal-live.txt&workspace_id=${terminal.workspace_id}`)
    if (!response.ok()) return ''
    return (await response.json() as { text?: string }).text?.trim() ?? ''
  }).toBe('LIVE_TERMINAL')
  await page.reload()
  await page.getByRole('button', { name: '工作面板', exact: true }).click()
  await page.getByRole('tab', { name: '终端', exact: true }).click()
  await expect(pane.getByRole('status')).toHaveText('已连接')
  const listed = await (await page.request.get(`/api/terminals?workspace_id=${terminal.workspace_id}`)).json() as { items: { terminal_id: string; status: string }[] }
  expect(listed.items.filter(item => item.status === 'running').map(item => item.terminal_id)).toEqual([terminal.terminal_id])
  await page.screenshot({ path: testInfo.outputPath('running.png') })
  await pane.getByRole('button', { name: '结束', exact: true }).click()
  await expect(pane.getByRole('status')).toHaveText('终端已结束')
  expect(errors).toEqual([])
})
