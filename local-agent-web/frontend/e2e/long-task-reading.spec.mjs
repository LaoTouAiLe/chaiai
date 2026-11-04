/* global window, document, innerWidth, setInterval, clearInterval, performance */
import { test, expect } from '@playwright/test'
import { writeFileSync } from 'node:fs'

const logOf = page => page.getByRole('log', { name: '对话', exact: true })
const inputOf = page => page.getByRole('textbox', { name: '给智能体发消息', exact: true })
async function start(page, mode = '闲聊') {
  await page.request.post('/__acceptance/reset')
  await page.goto('/')
  const created = page.waitForResponse(response => response.url().endsWith('/api/sessions') && response.request().method() === 'POST')
  await page.getByRole('button', { name: '新建会话', exact: true }).click()
  const session = await (await created).json()
  await expect(inputOf(page)).toBeEnabled()
  const button = page.getByRole('group', { name: '选择会话模式' }).getByRole('button', { name: mode, exact: true })
  if (await button.getAttribute('aria-pressed') !== 'true') await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  return session.session_id
}
async function send(page, text) {
  await inputOf(page).fill(text)
  await page.getByRole('button', { name: '发送消息', exact: true }).click()
}
async function snapshot(page, session) {
  return (await page.request.get(`/api/sessions/${session}`)).json()
}
async function completed(page, session) {
  await expect.poll(async () => (await snapshot(page, session)).session_state).toBe('completed')
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toHaveCount(0)
}
async function bottomGap(log) {
  return log.evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight)
}
async function beginTrace(log) {
  await log.evaluate(el => {
    window.__readingTrace = []
    window.__readingTimer = setInterval(() => {
      window.__readingTrace.push({ time: performance.now(), top: el.scrollTop,
        max: el.scrollHeight - el.clientHeight, chars: el.textContent.length })
    }, 60)
  })
}
async function saveTrace(page, info, label) {
  const trace = await page.evaluate(() => { clearInterval(window.__readingTimer); return window.__readingTrace })
  writeFileSync(info.outputPath(label + '.json'), JSON.stringify(trace, null, 2))
  await page.screenshot({ path: info.outputPath(label + '.png') })
  return trace
}

test('长回答真实增量、始终跟随尾部，结束与刷新不重复', async ({ page }, info) => {
  const session = await start(page)
  await send(page, 'LONG_STREAM')
  const log = logOf(page)
  await expect(log).toContainText('第 05 段')
  await expect(log).not.toContainText('长回答结束标记。')
  await beginTrace(log)
  await completed(page, session)
  const trace = await saveTrace(page, info, 'following-tail')
  expect(new Set(trace.map(item => item.chars)).size).toBeGreaterThan(5)
  await expect.poll(() => bottomGap(log)).toBeLessThan(80)
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
  await page.reload()
  await expect(log.getByRole('heading', { name: '长任务阅读验证', exact: true })).toHaveCount(1)
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
})

test('生成期间向上阅读历史，后续增量与结束不拉回底部', async ({ page }, info) => {
  const session = await start(page)
  await send(page, 'LONG_STREAM')
  await completed(page, session)
  await send(page, 'HISTORY_STREAM')
  const log = logOf(page)
  await expect(log.locator('[data-turn-id]').last()).toContainText('第 03 段')
  await beginTrace(log)
  await log.hover()
  await page.mouse.wheel(0, -30_000)
  let trace
  try {
    await expect.poll(() => log.evaluate(el => el.scrollTop)).toBeLessThan(3)
    await completed(page, session)
  } finally {
    trace = await saveTrace(page, info, 'reading-history')
  }
  // The first sample can precede delivery of the wheel event.
  trace = trace.slice(trace.findIndex(item => item.top < 3))
  expect(new Set(trace.map(item => item.chars)).size).toBeGreaterThan(5)
  expect(Math.max(...trace.map(item => item.top))).toBeLessThanOrEqual(5)
  await expect(page.getByRole('button', { name: '滑到最底', exact: true })).toBeVisible()
})

test('点击滑到最底后能重新跟随仍在生成的回答', async ({ page }, info) => {
  const session = await start(page)
  await send(page, 'LONG_STREAM')
  const log = logOf(page)
  await expect(log).toContainText('第 16 段')
  await log.evaluate(el => { el.scrollTop = 0 })
  const jump = page.getByRole('button', { name: '滑到最底', exact: true })
  await expect(jump).toBeVisible()
  await beginTrace(log)
  let trace
  try {
    await jump.click()
    await expect.poll(() => bottomGap(log)).toBeLessThan(80)
    await completed(page, session)
  } finally {
    trace = await saveTrace(page, info, 'resume-following')
  }
  expect(new Set(trace.map(item => item.chars)).size).toBeGreaterThan(5)
  await expect.poll(() => bottomGap(log)).toBeLessThan(80)
})

test('最终回答出现后过程收起，展开和收起均保留正文与草稿', async ({ page }, info) => {
  const session = await start(page, 'Agent')
  await send(page, 'TOOL_LONG')
  const log = logOf(page)
  await expect(log.getByText('读取文件', { exact: true })).toBeVisible()
  await inputOf(page).fill('下一轮草稿保持不变')
  await beginTrace(log)
  await completed(page, session)
  const trace = await saveTrace(page, info, 'process-settled')
  const process = log.getByRole('button', { name: /处理过程（/ })
  await expect(process).toHaveAttribute('aria-expanded', 'false')
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
  await expect.poll(() => bottomGap(log)).toBeLessThan(80)
  await process.click()
  await expect(log.getByText('读取文件', { exact: true })).toBeVisible()
  await process.click()
  await expect(log.getByText('读取文件', { exact: true })).not.toBeVisible()
  await expect(inputOf(page)).toHaveValue('下一轮草稿保持不变')
  expect(trace.length).toBeGreaterThan(5)
})

test('流式中断后普通调用自动补全，刷新不重复正文', async ({ page }, info) => {
  const session = await start(page)
  await send(page, 'FAIL_STREAM')
  const log = logOf(page)
  await expect(log).toContainText('第 01 段')
  await completed(page, session)
  await expect(log.getByRole('heading', { name: '长任务阅读验证', exact: true })).toHaveCount(1)
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
  await page.screenshot({ path: info.outputPath('fallback-completed.png') })
  await page.reload()
  await expect(log.getByRole('heading', { name: '长任务阅读验证', exact: true })).toHaveCount(1)
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
})

test('流式与普通调用连续失败后显示原因，恢复后可重新发送同一问题', async ({ page }, info) => {
  const session = await start(page)
  await send(page, 'FAIL_PERSISTENT')
  const log = logOf(page)
  await expect.poll(async () => (await snapshot(page, session)).session_state).toBe('failed')
  await expect(log).toContainText('本轮模型调用失败')
  await expect(log).not.toContainText('长回答结束标记。')
  await expect(page.getByRole('button', { name: '停止生成', exact: true })).toHaveCount(0)
  await page.screenshot({ path: info.outputPath('failed-call.png') })
  await page.request.post('/__acceptance/recover')
  await send(page, 'FAIL_PERSISTENT')
  await completed(page, session)
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
  await page.reload()
  await expect(log).toContainText('本轮模型调用失败')
  await expect(log.getByText('长回答结束标记。', { exact: true })).toHaveCount(1)
})

test('手机长回答不横向溢出，停止后保留部分回答并可续发', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const session = await start(page)
  await send(page, 'LONG_STREAM')
  const log = logOf(page)
  await expect(log).toContainText('第 10 段')
  await page.getByRole('button', { name: '停止生成', exact: true }).click()
  await expect(page.getByText('已停止', { exact: true })).toBeVisible()
  await expect.poll(async () => (await snapshot(page, session)).final_result?.state).toBe('cancelled')
  await expect(log).not.toContainText('长回答结束标记。')
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
  await page.screenshot({ path: info.outputPath('mobile-stopped.png') })
  await send(page, '手机停止后继续')
  await completed(page, session)
  await expect(log).toContainText('第三句后台回答在最后完成。')
})
