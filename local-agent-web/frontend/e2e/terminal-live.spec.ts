import { test, expect } from '@playwright/test'

test('真实受控终端：打开即连接、输入、刷新重连与关闭结束', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
  await page.goto('/')
  const beta = page.getByRole('button', { name: /知道了|开始使用/ })
  if (await beta.count()) await beta.first().click()
  await page.getByRole('button', { name: '新建会话', exact: true }).click()
  await page.getByRole('button', { name: '工作面板', exact: true }).click()
  let output = ''
  page.on('websocket', socket => socket.on('framereceived', event => {
    if (typeof event.payload !== 'string') output += event.payload.toString('utf8')
  }))
  const started = page.waitForResponse(response => response.url().endsWith('/api/terminals/requests') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '终端', exact: true }).press('Enter')
  const terminal = await (await started).json() as { terminal_id: string; workspace_id: string; status: string }
  const pane = page.getByRole('region', { name: '终端', exact: true })
  expect(terminal.status).toBe('running')
  await expect(pane.getByRole('status')).toHaveText('已连接')
  await expect.poll(() => output, { timeout: 45_000 }).toContain('>')
  const input = pane.locator('textarea.xterm-helper-textarea')
  await input.focus()
  await page.keyboard.type('cmd /d /c "echo LIVE_TERMINAL>terminal-live.txt"')
  await page.keyboard.press('Enter')
  // Assert a filesystem side effect from the real process, not echoed input text.
  await expect.poll(async () => {
    const response = await page.request.get(`/api/data-center/artifacts/preview?path=terminal-live.txt&workspace_id=${terminal.workspace_id}`)
    if (!response.ok()) return ''
    return (await response.json() as { text?: string }).text?.trim() ?? ''
  }).toBe('LIVE_TERMINAL')
  await page.reload()
  await page.getByRole('button', { name: '工作面板', exact: true }).click()
  await page.getByRole('tab', { name: /cmd|powershell|pwsh/ }).click()
  await expect(pane.getByRole('status')).toHaveText('已连接')
  const listed = await (await page.request.get(`/api/terminals?workspace_id=${terminal.workspace_id}`)).json() as { items: { terminal_id: string; status: string }[] }
  expect(listed.items.filter(item => item.status === 'running').map(item => item.terminal_id)).toEqual([terminal.terminal_id])
  await page.screenshot({ path: testInfo.outputPath('running.png') })
  const closed = page.waitForResponse(response => response.url().endsWith(`/api/terminals/${terminal.terminal_id}`) && response.request().method() === 'DELETE')
  await page.getByRole('button', { name: /^关闭(?:cmd|powershell|pwsh)页$/ }).click()
  expect((await closed).ok()).toBe(true)
  await expect(pane).toHaveCount(0)
  expect(errors).toEqual([])
})
