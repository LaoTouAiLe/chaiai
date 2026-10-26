import { expect, test, type Locator, type Page } from '@playwright/test'

const local = 'http://localhost:8147/'

async function open(page: Page, withSteps = false) {
  const title = `浏览器验收 ${Date.now()}`
  const created = await page.request.post('/api/sessions', { data: { title } })
  expect(created.ok()).toBe(true)
  const errors: string[] = []
  const requests = new Set<string>()
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname === '/api/browsers') {
      requests.add((request.postDataJSON() as { request_id: string }).request_id)
    }
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()
  await page.getByRole('button', { name: '工作面板', exact: true }).click()
  const panel = page.getByRole('complementary', { name: '工作面板', exact: true })
  if (withSteps) {
    await panel.getByRole('button', { name: '步骤', exact: true }).click()
    await panel.getByRole('button', { name: '新建页签' }).click()
  }
  const response = page.waitForResponse(response => response.request().method() === 'POST' && new URL(response.url()).pathname === '/api/browsers')
  await panel.getByRole('button', { name: '浏览器', exact: true }).click()
  const body = await (await response).json() as { browser_id: string }
  await expect(panel.getByRole('button', { name: '刷新网页' })).toBeEnabled()
  return { panel, errors, id: body.browser_id, cleanup: async () => {
    for (const id of requests) await page.request.delete(`/api/browsers/requests/${encodeURIComponent(id)}`)
  } }
}

async function go(panel: Locator, url: string) {
  const address = panel.getByRole('textbox', { name: '浏览器地址' })
  await address.fill(url)
  await address.press('Enter')
}

async function clickPage(panel: Locator, x: number, y: number) {
  await panel.getByLabel('网页交互区域', { exact: true }).click({ position: { x, y } })
}

test('真实浏览器：本地预览、中文、登录、历史导航、标签恢复和关闭清理', async ({ page }, info) => {
  const state = await open(page)
  const { panel } = state
  try {
    await go(panel, 'localhost:8147')
    await expect(panel.getByRole('tab', { name: '本地预览验收', exact: true })).toBeVisible()
    const frame = panel.getByAltText('浏览器网页画面')
    await expect(frame).toBeVisible()
    await expect.poll(() => frame.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(240)
    await clickPage(panel, 70, 110)
    const keyboard = panel.getByRole('textbox', { name: '网页键盘输入' })
    await expect(keyboard).toBeFocused()
    await keyboard.pressSequentially('draft')
    await keyboard.press('Control+A')
    await keyboard.dispatchEvent('compositionstart')
    await keyboard.fill('中文验收')
    await keyboard.dispatchEvent('compositionend', { data: '中文验收' })
    await keyboard.press('Enter')
    await expect(panel.getByRole('tab', { name: '回车:中文验收', exact: true })).toBeVisible()
    await clickPage(panel, 70, 166)
    await expect(panel.getByRole('tab', { name: '已登录:中文验收', exact: true })).toBeVisible()
    await clickPage(panel, 70, 226)
    await expect(panel.getByRole('tab', { name: '第二页预览', exact: true })).toBeVisible()
    await panel.getByRole('button', { name: '浏览器后退' }).click()
    await expect(panel.getByRole('tab', { name: '已登录:中文验收', exact: true })).toBeVisible()
    await panel.getByRole('button', { name: '浏览器前进' }).click()
    await expect(panel.getByRole('tab', { name: '第二页预览', exact: true })).toBeVisible()
    await go(panel, local)
    await expect(panel.getByRole('tab', { name: '已登录项目', exact: true })).toBeVisible()
    await panel.getByRole('button', { name: '刷新网页' }).click()
    await expect(panel.getByRole('tab', { name: '已登录项目', exact: true })).toBeVisible()
    await panel.getByRole('button', { name: '新建页签' }).click()
    await panel.getByRole('button', { name: '步骤', exact: true }).click()
    await panel.getByRole('tab', { name: '已登录项目', exact: true }).click()
    await expect(panel.getByRole('button', { name: '刷新网页' })).toBeEnabled()
    await expect(panel.getByRole('textbox', { name: '浏览器地址' })).toHaveValue(local)
    await panel.getByRole('button', { name: '关闭面板' }).click()
    await expect(panel).toHaveCount(0)
    await page.getByRole('button', { name: '工作面板', exact: true }).click()
    await expect(panel.getByRole('button', { name: '刷新网页' })).toBeEnabled()
    await expect(panel.getByRole('tab', { name: '已登录项目', exact: true })).toBeVisible()
    await page.screenshot({ path: info.outputPath('browser-local-desktop.png') })
    await clickPage(panel, 70, 274)
    await expect(panel.getByRole('dialog', { name: '网页提示' })).toContainText('网页提示正常')
    await panel.getByRole('button', { name: '确定', exact: true }).click()
    await expect(panel.getByRole('dialog')).toHaveCount(0, { timeout: 2000 })
    await expect(panel.getByRole('alert')).toHaveCount(0)
    await panel.getByLabel('网页交互区域', { exact: true }).hover()
    const beforeScroll = await frame.getAttribute('src')
    await page.mouse.wheel(0, 1000)
    await expect.poll(() => frame.getAttribute('src')).not.toBe(beforeScroll)
    await page.screenshot({ path: info.outputPath('browser-local-scrolled.png') })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.emulateMedia({ colorScheme: 'dark' })
    await expect.poll(() => frame.evaluate(element => {
      const image = element as HTMLImageElement
      return image.naturalWidth === Math.max(240, Math.min(1920, image.parentElement!.clientWidth))
    })).toBe(true)
    await expect(panel.getByRole('alert')).toHaveCount(0)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: info.outputPath('browser-local-mobile.png') })
    await panel.getByRole('button', { name: '关闭已登录项目页', exact: true }).click()
    await expect.poll(async () => (await page.request.get(`/api/browsers/${state.id}`)).status()).toBe(404)
    expect(state.errors).toEqual([])
  } finally { await state.cleanup() }
})

test('真实浏览器：网址校验、加载失败恢复与外网网页', async ({ page }, info) => {
  const state = await open(page)
  const { panel } = state
  try {
    await go(panel, 'file:///C:/Windows')
    await expect(panel.getByRole('alert')).toContainText('请输入 http/https')
    await go(panel, 'http://127.0.0.1:1')
    await expect(panel.getByRole('alert')).toContainText('ERR_UNSAFE_PORT')
    await go(panel, local)
    await expect(panel.getByRole('tab', { name: '本地预览验收', exact: true })).toBeVisible()
    await expect(panel.getByRole('alert')).toHaveCount(0)
    await go(panel, 'example.org:443')
    await expect(panel.getByRole('tab', { name: 'Example Domain', exact: true })).toBeVisible({ timeout: 45000 })
    await expect(panel.getByAltText('浏览器网页画面')).toBeVisible()
    await page.screenshot({ path: info.outputPath('browser-external.png') })
    expect(state.errors).toEqual([])
  } finally { await state.cleanup() }
})

test('真实浏览器：双击按原生点击次数传递，保留两次点击和一个双击', async ({ page }) => {
  const state = await open(page)
  try {
    await go(state.panel, `${local}double-click`)
    await expect(state.panel.getByRole('tab', { name: '双击验收', exact: true })).toBeVisible()
    const frame = state.panel.getByAltText('浏览器网页画面')
    await expect.poll(() => frame.evaluate(element => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(240)
    await state.panel.getByLabel('网页交互区域', { exact: true }).dblclick({ position: { x: 100, y: 60 } })
    await expect(state.panel.getByRole('tab', { name: '双击:2', exact: true })).toBeVisible()
    await expect(state.panel.getByRole('alert')).toHaveCount(0)
    expect(state.errors).toEqual([])
  } finally { await state.cleanup() }
})

test('真实浏览器：网页标题触发标签溢出后不抢走地址栏输入焦点', async ({ page }) => {
  const state = await open(page, true)
  const { panel } = state
  try {
    await go(panel, `${local}title-change`)
    await expect(panel.getByRole('tab', { name: '短', exact: true })).toBeVisible()
    await expect.poll(() => panel.getByAltText('浏览器网页画面').evaluate(element => (element as HTMLImageElement).naturalWidth)).toBeGreaterThan(240)
    await panel.getByRole('separator', { name: '拖动调整面板宽度' }).press('Home')
    await expect(panel.getByRole('button', { name: '向右滚动标签' })).toHaveCount(0)
    await clickPage(panel, 100, 60)
    const address = panel.getByRole('textbox', { name: '浏览器地址' })
    await address.fill('localhost:5173/draft')
    await expect(panel.getByRole('tab', { name: '一个足够长的网页标题用于触发标签栏溢出状态变化', exact: true })).toBeVisible()
    await expect(panel.getByRole('button', { name: '向右滚动标签' })).toBeVisible()
    await expect(address).toBeFocused()
    await page.keyboard.type('-continued')
    await expect(address).toHaveValue('localhost:5173/draft-continued')
    expect(state.errors).toEqual([])
  } finally { await state.cleanup() }
})
