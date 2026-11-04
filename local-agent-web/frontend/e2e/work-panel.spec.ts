import { readFileSync } from 'node:fs'
import { expect, test, type Page, type Route } from '@playwright/test'
import type { components } from '../src/api/schema.gen'

const workspace = { workspace_id: 'panel-workspace', display_name: 'chaiai' }
const snapshot: components['schemas']['SessionSnapshot'] = {
  schema_version: 1, session_id: 'panel-session', snapshot_id: 'panel-snapshot', title: '优化工作面板',
  mode: 'chat', branch_id: 'main', branch_title: '主分支', workspace, session_state: 'completed',
  created_at: '2026-10-25T00:00:00Z', answer_streaming: false,
  messages: [
    { kind: 'user', message_id: 'question', text: '帮我查看项目文件与执行过程。' },
    { kind: 'tool', message_id: 'read', tool: { tool_call_id: 'read-1', tool_name: 'read_file',
      status: 'ok', redacted_arguments: { path: 'README.md' }, elapsed_ms: 8, truncated: false, output_preview: '项目说明',
      delegation_scope: '' } },
    { kind: 'assistant', message_id: 'answer', text: '可以在右侧工作面板查看文件、改动和执行步骤。',
      complete: true, chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text' },
  ],
}

const changedPaths = ['README.md', 'src/main.ts', 'docs/generated.txt']
const changes: components['schemas']['SessionChangesView'] = {
  session_id: snapshot.session_id, total_files: 3, additions: 3, deletions: 3,
  turns: [{ run_id: 'change-run', started_at: snapshot.created_at, files: changedPaths.map(path => ({
    path, kind: 'modify', additions: 1, deletions: 1, edits: 1, last_at: snapshot.created_at, has_baseline: true,
  })) }], unrecorded: [],
}

async function setup(page: Page, withChanges = false, replyText?: string, previewFiles?: string[]) {
  let filesFail = false
  let previewRequests = 0
  const searchWorkspaces: (string | null)[] = []
  const previewPaths: { path: string | null; workspace: string | null }[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/health') return route.fulfill({ json: { model_configured: true } })
    if (path === '/api/preferences') return route.fulfill({ json: { values: { general: { theme: 'system', animations: true } } } })
    if (path === '/api/workspaces') return route.fulfill({ json: [workspace] })
    if (path === '/api/tasks') return route.fulfill({ json: { tasks: [], pending_count: 0 } })
    if (path === '/api/model-connections') return route.fulfill({ json: [] })
    if (path === '/api/sessions') return route.fulfill({ json: [{ session_id: snapshot.session_id,
      title: snapshot.title, workspace, mode: 'chat', is_active: true, archived: false,
      created_at: snapshot.created_at, updated_at: snapshot.created_at }] })
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: ': ready\n\n' })
    if (path === '/api/sessions/panel-session') return route.fulfill({ json: replyText === undefined ? snapshot : {
      ...snapshot, messages: snapshot.messages?.map(message => message.kind === 'assistant' ? { ...message, text: replyText } : message),
    } })
    if (path.endsWith('/changes')) return route.fulfill({ json: withChanges ? changes : { turns: [], unrecorded: [] } })
    if (path.endsWith('/changes/diff')) {
      const file = url.searchParams.get('path') ?? ''
      const diff: components['schemas']['FileChangeDiffView'] = {
        path: file, has_baseline: file !== 'docs/generated.txt', additions: 1, deletions: 1, truncated: false,
        unavailable_reason: '此文件未记录改前内容。',
        hunks: [{ header: '@@ -1 +1 @@', lines: [
          { kind: 'del', old_line: 1, text: `before ${file}` },
          { kind: 'add', new_line: 1, text: `after ${file} ${'long line '.repeat(40)}` },
        ] }],
      }
      return route.fulfill({ json: diff })
    }
    if (path === '/api/workspace/files' && previewFiles) return route.fulfill({ json: { entries: previewFiles.map(name => ({ name, path: name, kind: 'file', size_bytes: 1000 })), truncated: false } })
    if (path === '/api/workspace/files') return route.fulfill(filesFail
      ? { status: 500, json: { detail: 'test read failure' } }
      : { json: { entries: [
        { name: 'src', path: 'src', kind: 'dir' },
        { name: 'main.ts', path: 'src/main.ts', kind: 'file', size_bytes: 128 },
        { name: 'README.md', path: 'README.md', kind: 'file', size_bytes: 1024 },
      ], truncated: false } })
    if (path === '/api/workspace/files/search') {
      searchWorkspaces.push(url.searchParams.get('workspace_id'))
      return route.fulfill({ json: { entries: [], truncated: false } })
    }
    if (path === '/api/data-center/artifacts/preview') {
      previewRequests += 1
      previewPaths.push({ path: url.searchParams.get('path'), workspace: url.searchParams.get('workspace_id') })
      if (url.searchParams.get('path') === 'src/missing.ts') return route.fulfill({ status: 404, json: { detail: '文件不存在或已被删除。' } })
      if (url.searchParams.get('path') === '../outside.txt') return route.fulfill({ status: 403, json: { detail: '只能预览工作区内的文件。' } })
      return route.fulfill({ json: { kind: 'text', text: '# chaiai\n\n工作面板预览内容。', size_bytes: 48, truncated: false } })
    }
    if (path === '/api/open-in-app/apps') return route.fulfill({ json: { available: true, apps: [
      { id: 'explorer', label: '文件资源管理器', icon_url: '/api/open-in-app/icon/explorer' },
      { id: 'cursor', label: 'Cursor', icon_url: '/api/open-in-app/icon/cursor' },
    ] } })
    if (path.startsWith('/api/open-in-app/icon/')) return route.fulfill({ status: 404, json: { detail: '没有取到这个应用的图标。' } })
    if (path === '/api/open-in-app/open') return route.fulfill({ json: { ok: true, app_id: 'cursor', app_label: 'Cursor' } })
    if (path === '/api/terminals') return route.fulfill({ json: { available: false, items: [], reason: '此测试不启动真实终端' } })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: snapshot.title, exact: true })).toBeVisible()
  const trigger = page.getByRole('button', { name: '工作面板', exact: true })
  await trigger.click()
  const panel = page.getByRole('complementary', { name: '工作面板', exact: true })
  await expect(panel.getByRole('navigation')).toBeVisible()
  return { panel, trigger, errors, searchWorkspaces, previewPaths, setFilesFail: (value: boolean) => { filesFail = value }, previews: () => previewRequests }
}

async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0)
  const panel = page.getByRole('complementary', { name: '工作面板', exact: true })
  expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth + 2)).toBe(true)
}

test('工作面板：入口、文件预览、标签键盘导航和关闭恢复', async ({ page }, info) => {
  const { panel, trigger, errors } = await setup(page)
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-home.png') })
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /README.md/ }).click()
  await expect(panel.getByRole('tab', { name: 'README.md', exact: true })).toBeVisible()
  await expect(panel.getByText('工作面板预览内容。', { exact: false })).toBeVisible()
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-files.png') })
  // 打开方式（2026-11-03）：菜单展开状态截一张，图标取不到时画首字母块。
  await panel.getByRole('button', { name: '打开方式' }).click()
  await expect(panel.getByRole('menuitem', { name: /Cursor/ })).toBeVisible()
  await page.screenshot({ path: info.outputPath('panel-open-in-app.png') })
  await page.keyboard.press('Escape')
  await panel.getByRole('button', { name: '新建页签' }).click()
  await panel.getByRole('button', { name: '步骤', exact: true }).click()
  await expect(panel.getByRole('button', { name: /帮我查看项目文件与执行过程/ })).toBeVisible()
  await page.screenshot({ path: info.outputPath('panel-steps.png') })
  await page.keyboard.press('ArrowLeft')
  await expect(panel.getByRole('tab', { name: 'README.md' })).toBeFocused()
  await page.keyboard.press('End')
  await expect(panel.getByRole('tab', { name: '步骤', exact: true })).toBeFocused()
  await page.keyboard.press('Delete')
  await expect(panel.getByRole('tab', { name: '步骤', exact: true })).toHaveCount(0)
  await page.keyboard.press('Escape')
  await expect(panel).toHaveCount(0)
  await expect(trigger).toBeFocused()
  await trigger.click()
  await expect(panel.getByRole('tab', { name: 'README.md' })).toHaveAttribute('aria-selected', 'true')
  await page.reload()
  await expect(page.getByRole('heading', { name: snapshot.title, exact: true })).toBeVisible()
  await trigger.click()
  await expect(panel.getByRole('tab', { name: 'README.md' })).toHaveAttribute('aria-selected', 'true')
  expect(errors).toEqual([])
})

test('工作面板：读取失败可重试、搜索空态与改动空态', async ({ page }, info) => {
  const state = await setup(page)
  state.setFilesFail(true)
  await state.panel.getByRole('button', { name: '文件', exact: true }).click()
  await expect(state.panel.getByRole('alert')).toContainText('文件树读取失败', { timeout: 10000 })
  await expect(state.panel.getByText('这个工作区里还没有文件。')).toHaveCount(0)
  state.setFilesFail(false)
  await state.panel.getByRole('button', { name: '重试', exact: true }).click()
  await expect(state.panel.getByRole('button', { name: /README.md/ })).toBeVisible()
  await state.panel.getByRole('searchbox', { name: '搜索文件名' }).fill('missing')
  await expect(state.panel.getByText('没有匹配「missing」的文件。')).toBeVisible()
  expect(state.searchWorkspaces).toEqual([workspace.workspace_id])
  await state.panel.getByRole('button', { name: '新建页签' }).click()
  await state.panel.getByRole('button', { name: '改动', exact: true }).click()
  await expect(state.panel.getByText('还没有文件改动')).toBeVisible()
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-empty.png') })
  expect(state.errors).toEqual([])
})

test('工作面板：鼠标拖宽、键盘边界和双击复位', async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 0) <= 480, '全宽面板不显示宽度手柄')
  const { panel, errors } = await setup(page)
  const handle = panel.getByRole('separator', { name: '拖动调整面板宽度', exact: true })
  const box = (await handle.boundingBox())!
  await page.mouse.move(box.x + box.width / 2, box.y + 100)
  await page.mouse.down()
  await page.mouse.move(box.x - 60, box.y + 100, { steps: 8 })
  await page.mouse.up()
  expect(Number(await handle.getAttribute('aria-valuenow'))).toBeGreaterThan(560)
  await handle.press('Home')
  await expect(handle).toHaveAttribute('aria-valuenow', '320')
  await handle.press('ArrowRight')
  await expect(handle).toHaveAttribute('aria-valuenow', '320')
  await handle.dblclick()
  await expect(handle).toHaveAttribute('aria-valuenow', '520')
  expect(await page.evaluate(() => document.body.style.userSelect)).toBe('')
  await noOverflow(page)
  expect(errors).toEqual([])
})

test('工作面板：深色主题、手机布局和减少动态效果', async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' })
  const { panel, errors } = await setup(page)
  expect(await panel.evaluate(element => getComputedStyle(element).animationName)).toBe('none')
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-dark-mobile.png') })
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /README.md/ }).click()
  await expect(panel.getByText('工作面板预览内容。', { exact: false })).toBeVisible()
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-files-mobile.png') })
  expect(errors).toEqual([])
})

test('工作面板：完整动效和设置中的减少动画均生效', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const { panel, trigger } = await setup(page)
  expect(await panel.evaluate(element => getComputedStyle(element).animationName)).not.toBe('none')
  await panel.getByRole('button', { name: '关闭面板' }).click()
  await expect(panel).toHaveCount(0)
  await trigger.click()
  await expect(panel.getByRole('navigation')).toBeVisible()
  await page.evaluate(() => { document.documentElement.dataset.motion = 'reduced' })
  expect(await panel.evaluate(element => getComputedStyle(element).animationName)).toBe('none')
  expect(await panel.getByRole('button', { name: '文件', exact: true }).evaluate(element => getComputedStyle(element).animationName)).toBe('none')
})

test('改动页：目录常驻、切换对比、筛选、调宽及手机布局', async ({ page }, info) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  const { panel, errors } = await setup(page, true)
  await panel.getByRole('button', { name: '改动', exact: true }).click()
  const directory = panel.getByRole('complementary', { name: '改动目录' })
  const detail = panel.getByRole('region', { name: '改动详情' })
  await directory.getByRole('button', { name: 'README.md', exact: true }).click()
  await expect(detail.getByText('before README.md', { exact: true })).toBeVisible()
  await expect(directory).toBeVisible()
  const left = (await detail.boundingBox())!
  const right = (await directory.boundingBox())!
  expect(right.x).toBeGreaterThanOrEqual(left.x + left.width)
  const handle = panel.getByRole('separator', { name: '拖动调整改动目录宽度' })
  await handle.press('ArrowLeft')
  await expect(handle).toHaveAttribute('aria-valuenow', '236')
  const drag = (await handle.boundingBox())!
  await page.mouse.move(drag.x + drag.width / 2, drag.y + 20)
  await page.mouse.down()
  await page.mouse.move(drag.x - 24, drag.y + 20, { steps: 4 })
  await page.mouse.up()
  expect(Number(await handle.getAttribute('aria-valuenow'))).toBeGreaterThan(250)
  await handle.dblclick()
  await expect(handle).toHaveAttribute('aria-valuenow', '220')
  const filter = directory.getByRole('searchbox', { name: '按路径筛选改动' })
  await filter.fill('main')
  await directory.getByRole('button', { name: 'main.ts', exact: true }).click()
  await expect(detail.getByText('before src/main.ts', { exact: true })).toBeVisible()
  await expect(filter).toHaveValue('main')
  await filter.fill('missing')
  await expect(directory.getByText('没有匹配的改动')).toBeVisible()
  await expect(detail.getByText('before src/main.ts', { exact: true })).toBeVisible()
  await filter.fill('')
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-changes-split.png'), animations: 'disabled' })
  await directory.getByRole('button', { name: 'generated.txt', exact: true }).click()
  await expect(detail.getByText('此文件未记录改前内容。')).toBeVisible()
  await panel.getByRole('button', { name: '关闭改动详情' }).click()
  await expect(detail.getByText('选择一个改动文件')).toBeVisible()
  await directory.getByRole('button', { name: 'main.ts', exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.emulateMedia({ colorScheme: 'dark' })
  await expect(detail.getByText('before src/main.ts', { exact: true })).toBeVisible()
  const mobileDetail = (await detail.boundingBox())!
  const mobileDirectory = (await directory.boundingBox())!
  expect(mobileDirectory.y).toBeGreaterThanOrEqual(mobileDetail.y + mobileDetail.height)
  await expect(handle).toBeHidden()
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-changes-mobile.png'), animations: 'disabled' })
  expect(errors).toEqual([])
})

test('标签：横向滑入、超宽滚动按钮及键盘定位', async ({ page }, info) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  const { panel, errors } = await setup(page)
  for (let index = 0; index < 8; index += 1) {
    if (index > 0) await panel.getByRole('button', { name: '新建页签' }).click()
    await panel.getByRole('button', { name: '步骤', exact: true }).click()
  }
  const strip = panel.getByRole('tablist')
  const tabs = strip.getByRole('tab')
  const content = panel.getByRole('tabpanel')
  const sampleSlide = () => content.evaluate(async element => {
    const animation = element.getAnimations()[0]
    if (!animation?.effect) throw new Error('没有内容切换动画')
    animation.pause()
    animation.currentTime = Number(animation.effect.getTiming().duration) / 2
    await new Promise(requestAnimationFrame)
    const style = getComputedStyle(element)
    const sample = { x: new DOMMatrixReadOnly(style.transform).m41, opacity: Number(style.opacity) }
    animation.finish()
    return sample
  })
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeGreaterThan(100)
  await tabs.last().press('Home')
  await expect(tabs.first()).toHaveAttribute('aria-selected', 'true')
  await expect(content).toHaveAttribute('data-slide-direction', 'left')
  expect(await content.evaluate(element => getComputedStyle(element).animationName)).toContain('tab-slide-in')
  const leftFrame = await sampleSlide()
  expect(leftFrame.x).toBeLessThan(0)
  expect(leftFrame.opacity).toBeGreaterThan(0)
  expect(leftFrame.opacity).toBeLessThan(1)
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeLessThan(2)
  await expect(panel.getByRole('button', { name: '向右滚动标签' })).toBeVisible()
  await tabs.first().press('End')
  await expect(content).toHaveAttribute('data-slide-direction', 'right')
  expect((await sampleSlide()).x).toBeGreaterThan(0)
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeGreaterThan(100)
  await expect(panel.getByRole('button', { name: '向右滚动标签' })).toHaveAttribute('aria-disabled', 'true')
  const end = await strip.evaluate(element => element.scrollLeft)
  await panel.getByRole('button', { name: '向左滚动标签' }).click()
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeLessThan(end - 50)
  await panel.getByRole('button', { name: '向右滚动标签' }).click()
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeGreaterThan(end - 2)
  await expect(panel.getByRole('button', { name: '向右滚动标签' })).toBeFocused()
  await expect(panel.getByRole('button', { name: '向右滚动标签' })).toHaveAttribute('aria-disabled', 'true')
  await noOverflow(page)
  await page.screenshot({ path: info.outputPath('panel-tabs-scroll.png'), animations: 'disabled' })
  await page.evaluate(() => { document.documentElement.dataset.motion = 'reduced' })
  await tabs.last().press('Home')
  expect(await content.evaluate(element => getComputedStyle(element).animationName)).toBe('none')
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeLessThan(2)
  // 普通鼠标滚轮映射到横向，边界不把纵向滚动传给页面。
  await strip.hover()
  await page.mouse.wheel(0, 120)
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeGreaterThan(50)
  await page.mouse.wheel(0, -10000)
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeLessThan(2)
  const pageTop = await page.evaluate(() => scrollY)
  await page.mouse.wheel(0, -120)
  expect(await page.evaluate(() => scrollY)).toBe(pageTop)
  await page.mouse.wheel(0, 10000)
  await expect(panel.getByRole('button', { name: '向右滚动标签' })).toHaveAttribute('aria-disabled', 'true')
  const atEnd = await strip.evaluate(element => element.scrollLeft)
  await page.mouse.wheel(0, 120)
  expect(await strip.evaluate(element => element.scrollLeft)).toBe(atEnd)
  // 原生横向触控板滚动仍可用。
  await page.mouse.wheel(-120, 0)
  await expect.poll(() => strip.evaluate(element => element.scrollLeft)).toBeLessThan(atEnd - 50)
  expect(await strip.evaluate(element => {
    const zoom = new WheelEvent('wheel', { deltaY: 120, ctrlKey: true, cancelable: true, bubbles: true })
    return element.dispatchEvent(zoom)
  })).toBe(true)
  expect(errors).toEqual([])
})

test('回答文件链接：自动展开右栏、独立标签、重复复用、空格路径及失败提示', async ({ page }, info) => {
  const state = await setup(page, false, '**改动文件**：[浏览器界面](E:/chaiai/src/BrowserPane.tsx)、[后端服务](E:/chaiai/src/browsers.py)、[空格文件](<file:///E:/chaiai/My%20Notes.md>)。\n\n[相对路径](README.md)、[另一个写法](./README.md)、[不存在](src/missing.ts)、[越界](../outside.txt)、[外部站点](https://example.org/)')
  const { panel } = state
  await panel.getByRole('button', { name: '关闭面板' }).click()
  await expect(panel).toHaveCount(0)
  const originalUrl = page.url()
  const pageCount = page.context().pages().length
  const clickReference = async (name: string) => {
    if ((page.viewportSize()?.width ?? 0) <= 1100 && await panel.count()) {
      await panel.getByRole('button', { name: '关闭面板' }).click()
      await expect(panel).toHaveCount(0)
    }
    await page.getByRole('link', { name, exact: true }).click()
  }
  await clickReference('浏览器界面')
  await expect(panel.getByRole('tab', { name: 'BrowserPane.tsx', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(panel.getByRole('tabpanel')).toContainText('工作面板预览内容。')
  await clickReference('后端服务')
  await expect(panel.getByRole('tab', { name: 'browsers.py', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(panel.getByRole('tab')).toHaveCount(2)
  await clickReference('浏览器界面')
  await expect(panel.getByRole('tab')).toHaveCount(2)
  await expect(panel.getByRole('tab', { name: 'BrowserPane.tsx' })).toHaveAttribute('aria-selected', 'true')
  await clickReference('空格文件')
  await expect(panel.getByRole('tab', { name: 'My Notes.md', exact: true })).toBeVisible()
  await expect.poll(() => state.previewPaths.some(item => item.path === 'E:/chaiai/My Notes.md')).toBe(true)
  await clickReference('相对路径')
  await expect(panel.getByRole('tab', { name: 'README.md', exact: true })).toBeVisible()
  await clickReference('另一个写法')
  await expect(panel.getByRole('tab')).toHaveCount(4)
  await expect(panel.getByRole('tab', { name: 'README.md', exact: true })).toHaveAttribute('aria-selected', 'true')
  await page.screenshot({ path: info.outputPath('reply-file-links.png') })
  await clickReference('不存在')
  await expect(panel.getByRole('alert')).toContainText('文件不存在或已被删除。', { timeout: 10000 })
  await expect(panel.getByRole('button', { name: '重试', exact: true })).toBeVisible()
  await clickReference('越界')
  await expect(panel.getByRole('alert')).toContainText('只能预览工作区内的文件。', { timeout: 10000 })
  expect(state.previewPaths.every(item => item.workspace === workspace.workspace_id)).toBe(true)
  await expect(page.getByRole('link', { name: '外部站点', exact: true })).toHaveAttribute('target', '_blank')
  expect(page.context().pages()).toHaveLength(pageCount)
  expect(page.url()).toBe(originalUrl)
  await noOverflow(page)
  expect(state.errors).toEqual([])
})

test('侧聊：创建中切换和收起只建一次，关闭后独立创建，失败可重试', async ({ page }) => {
  const { panel, trigger, errors } = await setup(page)
  const creations: Route[] = []
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/side-chats')) { creations.push(route); return }
    if (/^\/api\/sessions\/slow-side-\d+$/.test(path)) return route.fulfill({ json: {
      ...snapshot, session_id: path.split('/').at(-1), title: '侧聊创建验收', messages: [],
    } })
    return route.fallback()
  })
  await panel.getByRole('button', { name: '侧边对话', exact: true }).click()
  await expect.poll(() => creations.length).toBe(1)
  await panel.getByRole('button', { name: '新建页签' }).click()
  await panel.getByRole('button', { name: '步骤', exact: true }).click()
  await panel.getByRole('tab', { name: '侧边对话', exact: true }).click()
  await expect(panel.getByText('正在准备侧边对话…')).toBeVisible()
  await panel.getByRole('button', { name: '关闭面板' }).click()
  await expect(panel).toHaveCount(0)
  await trigger.click()
  await expect(panel.getByText('正在准备侧边对话…')).toBeVisible()
  await creations[0].fulfill({ json: { session_id: 'slow-side-1', title: '已创建侧聊', workspace } })
  await expect(panel.getByRole('textbox', { name: '侧边对话输入框' })).toBeVisible()
  expect(creations).toHaveLength(1)
  await panel.getByRole('button', { name: '关闭侧边对话页', exact: true }).click()
  await panel.getByRole('button', { name: '新建页签' }).click()
  await panel.getByRole('button', { name: '侧边对话', exact: true }).click()
  await expect.poll(() => creations.length).toBe(2)
  await creations[1].fulfill({ status: 503, json: { detail: '创建失败验收' } })
  await expect(panel.getByText('没能开始侧边对话，请重试。')).toBeVisible()
  await panel.getByRole('button', { name: '重试', exact: true }).click()
  await expect.poll(() => creations.length).toBe(3)
  await creations[2].fulfill({ json: { session_id: 'slow-side-3', title: '重试后的独立侧聊', workspace } })
  await expect(panel.getByRole('textbox', { name: '侧边对话输入框' })).toBeVisible()
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('panel.state.v1:panel-session')!).tabs
    .filter((tab: { kind: string }) => tab.kind === 'sidechat').map((tab: { param: string }) => tab.param))).toEqual(['slow-side-3'])
  expect(errors).toEqual([])
})

test('侧聊：切换标签后失败重试保留同一个请求键', async ({ page }) => {
  const { panel, errors } = await setup(page)
  const submissions: { text: string; client_request_id: string }[] = []
  let failFirst: (() => Promise<void>) | undefined
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/side-chats')) return route.fulfill({ json: { session_id: 'side-retry', title: '重试侧聊', workspace } })
    if (path === '/api/sessions/side-retry') return route.fulfill({ json: {
      ...snapshot, session_id: 'side-retry', title: '重试侧聊', messages: [],
    } })
    if (path === '/api/sessions/side-retry/messages') {
      submissions.push(route.request().postDataJSON() as typeof submissions[number])
      if (submissions.length === 1) { failFirst = () => route.fulfill({ status: 503, json: { detail: '响应丢失' } }); return }
      return route.fulfill({ status: 503, json: { detail: '重试验收' } })
    }
    return route.fallback()
  })
  await panel.getByRole('button', { name: '侧边对话', exact: true }).click()
  const input = panel.getByRole('textbox', { name: '侧边对话输入框' })
  await input.fill('同一次失败重试')
  await panel.getByRole('button', { name: '发送', exact: true }).click()
  await expect.poll(() => submissions.length).toBe(1)
  await panel.getByRole('button', { name: '新建页签' }).click()
  await panel.getByRole('button', { name: '步骤', exact: true }).click()
  await failFirst!()
  await panel.getByRole('tab', { name: '侧边对话', exact: true }).click()
  await expect(input).toHaveValue('同一次失败重试')
  await panel.getByRole('button', { name: '发送', exact: true }).click()
  await expect.poll(() => submissions.length).toBe(2)
  expect(submissions[1]).toEqual(submissions[0])
  await expect(panel.getByRole('alert')).toContainText('发送失败')
  expect(errors).toEqual([])
})

test('切换会话：文件打开指令不会污染另一会话的持久化标签', async ({ page }) => {
  const { errors } = await setup(page, false, '[打开A文件](E:/workspace-a/only-A.ts)')
  const workspaceB = { workspace_id: 'workspace-b', display_name: '工作区 B' }
  const snapshotB: components['schemas']['SessionSnapshot'] = {
    ...snapshot, session_id: 'panel-session-b', snapshot_id: 'snapshot-b', title: '独立会话 B', workspace: workspaceB,
    messages: [{ kind: 'user', message_id: 'question-b', text: 'B 会话的问题' }],
  }
  let active = snapshot.session_id
  const summary = (id: string) => {
    const selected = id === snapshot.session_id ? snapshot : snapshotB
    return { session_id: id, title: selected.title, workspace: selected.workspace, mode: 'chat',
      is_active: id === active, archived: false, created_at: snapshot.created_at, updated_at: snapshot.created_at }
  }
  await page.route('**/api/**', route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/workspaces') return route.fulfill({ json: [workspace, workspaceB] })
    if (path === '/api/sessions') return route.fulfill({ json: [summary(snapshot.session_id), summary(snapshotB.session_id)] })
    if (path === `/api/sessions/${snapshotB.session_id}`) return route.fulfill({ json: snapshotB })
    if (path === `/api/sessions/${snapshotB.session_id}/select` || path === `/api/sessions/${snapshot.session_id}/select`) {
      active = path.includes(snapshotB.session_id) ? snapshotB.session_id : snapshot.session_id
      return route.fulfill({ json: summary(active) })
    }
    return route.fallback()
  })
  await page.evaluate(() => localStorage.setItem('panel.state.v1:panel-session-b', JSON.stringify({
    version: 1, tabs: [{ id: 'files-20', kind: 'files', param: 'only-B.txt' }], activeId: 'files-20', width: 520,
  })))
  await page.reload()
  await page.getByRole('link', { name: '打开A文件', exact: true }).click()
  const panel = page.getByRole('complementary', { name: '工作面板', exact: true })
  await expect(panel.getByRole('tab', { name: 'only-A.ts' })).toBeVisible()
  if ((page.viewportSize()?.width ?? 0) <= 1100) {
    await panel.getByRole('button', { name: '关闭面板' }).click()
    await expect(panel).toHaveCount(0)
  }
  const expandSidebar = page.getByRole('button', { name: '展开侧边栏', exact: true })
  if (await expandSidebar.isVisible()) await expandSidebar.click()
  await page.getByRole('button', { name: /独立会话 B/ }).first().click()
  await expect(page.getByRole('heading', { name: snapshotB.title, exact: true })).toBeVisible()
  await expect(panel).toHaveCount(0)
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('panel.state.v1:panel-session-b')!).tabs.map((tab: { param: string }) => tab.param))).toEqual(['only-B.txt'])
  await page.getByRole('button', { name: '工作面板', exact: true }).click()
  await expect(panel.getByRole('tab', { name: 'only-B.txt' })).toBeVisible()
  await expect(panel.getByRole('tab', { name: 'only-A.ts' })).toHaveCount(0)
  expect(errors).toEqual([])
})


test('文件预览：Office 纸张版式、PDF 翻页缩放与文件树收起', async ({ page }, info) => {
  const { panel, errors } = await setup(page, false, undefined, ['report.docx'])
  await page.route('**/api/workspace/files?**', route => route.fulfill({ json: { entries: [
    { name: 'report.docx', path: 'report.docx', kind: 'file', size_bytes: 1000 },
  ], truncated: false } }))
  await page.route('**/api/data-center/artifacts/preview?**', route => route.fulfill({ json: {
    kind: 'document', url: '/api/data-center/artifacts/content?path=report.docx', size_bytes: 1000,
  } }))
  await page.route('**/api/data-center/artifacts/content?**', route => route.fulfill({
    contentType: 'application/pdf', body: readFileSync(new URL('./fixtures/panel-report.pdf', import.meta.url)),
  }))
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /^report\.docx/  }).click()
  await panel.getByRole('button', { name: '收起文件树', exact: true }).click()
  const canvas = panel.getByLabel('文档第 1 页', { exact: true })
  await expect(canvas).toBeVisible()
  await expect.poll(() => canvas.evaluate(el => (el as HTMLCanvasElement).width)).toBeGreaterThan(100)
  await expect(panel.getByText('1 / 2', { exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('office-document.png') })
  await panel.getByRole('button', { name: '下一页', exact: true }).click()
  await expect(panel.getByLabel('文档第 2 页')).toBeVisible()
  await panel.getByRole('button', { name: '放大文档', exact: true }).click()
  await expect(panel.getByRole('button', { name: '125%', exact: true })).toBeVisible()
  await panel.getByRole('button', { name: '展开文件树', exact: true }).click()
  await expect(panel.getByRole('button', { name: /^report\.docx/  })).toBeVisible()
  await noOverflow(page)
  expect(errors).toEqual([])
})

test('文件预览：Markdown 排版、HTML 隔离和源码切换', async ({ page }, info) => {
  const { panel, errors } = await setup(page, false, undefined, ['guide.md', 'page.html'])
  await page.route('**/api/workspace/files?**', route => route.fulfill({ json: { entries: [
    { name: 'guide.md', path: 'guide.md', kind: 'file', size_bytes: 100 },
    { name: 'page.html', path: 'page.html', kind: 'file', size_bytes: 100 },
  ], truncated: false } }))
  await page.route('**/api/data-center/artifacts/preview?**', route => route.fulfill({ json: {
    kind: 'text', text: new URL(route.request().url()).searchParams.get('path') === 'page.html'
      ? '<h1>网页预览</h1><script>parent.__previewEscaped=true</script>'
      : '# 预览指南\n\n| 名称 | 状态 |\n| --- | --- |\n| 文档 | 完成 |', size_bytes: 100,
  } }))
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /^guide\.md/  }).click()
  await expect(panel.getByRole('heading', { name: '预览指南' })).toBeVisible()
  await expect(panel.getByRole('table')).toBeVisible()
  await page.screenshot({ path: info.outputPath('markdown-document.png') })
  await panel.getByRole('button', { name: '源码', exact: true }).click()
  await expect(panel.getByText('# 预览指南', { exact: false })).toBeVisible()
  await panel.getByRole('button', { name: /^page\.html/  }).click()
  await expect(page.frameLocator('iframe[title="HTML 文件预览"]').getByRole('heading', { name: '网页预览' })).toBeVisible()
  expect(await page.evaluate(() => '__previewEscaped' in window)).toBe(false)
  await noOverflow(page)
  expect(errors).toEqual([])
})

test('文件预览：工作表切换和下一组行', async ({ page }, info) => {
  const { panel, errors } = await setup(page, false, undefined, ['budget.xlsx'])
  await page.route('**/api/workspace/files?**', route => route.fulfill({ json: { entries: [
    { name: 'budget.xlsx', path: 'budget.xlsx', kind: 'file', size_bytes: 100 },
  ], truncated: false } }))
  await page.route('**/api/data-center/artifacts/preview?**', route => {
    const params = new URL(route.request().url()).searchParams
    const summary = params.get('sheet') === '汇总'
    const next = Number(params.get('offset')) > 0
    return route.fulfill({ json: { kind: 'spreadsheet', size_bytes: 100,
      sheets: ['预算', '汇总'], sheet: summary ? '汇总' : '预算',
      rows: summary ? [['合计', '12800']] : next ? [['最后一行', '800']] : [['项目', '预算'], ['设计', '12000']],
      has_more: !summary && !next, next_offset: !summary && !next ? 200 : null,
    } })
  })
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /^budget\.xlsx/  }).click()
  await expect(panel.getByRole('table', { name: '预算', exact: true })).toContainText('设计')
  await page.screenshot({ path: info.outputPath('spreadsheet-document.png') })
  await panel.getByRole('button', { name: '下一组', exact: true }).click()
  await expect(panel.getByRole('table')).toContainText('最后一行')
  await panel.getByRole('tab', { name: '汇总', exact: true }).click()
  await expect(panel.getByRole('table', { name: '汇总', exact: true })).toContainText('12800')
  await noOverflow(page)
  expect(errors).toEqual([])
})


test('文件预览：真实空行保留，代码与纯文本单倍行高，顶部只有两行工具栏', async ({ page }, info) => {
  const { panel, errors } = await setup(page, false, undefined, ['pom.xml', 'plain.txt'])
  const source = ['<project>', '', ...Array.from({ length: 80 }, (_, i) => `  <item>${i}</item>`), '</project>'].join('\n')
  await page.route('**/api/data-center/artifacts/preview?**', route => route.fulfill({ json: { kind: 'text', text: source, size_bytes: source.length } }))
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /^pom\.xml/ }).click()
  const reader = panel.getByRole('region', { name: '文件预览', exact: true })
  const lines = reader.locator('pre code .line')
  await expect(lines).toHaveCount(83)
  const checkLines = async (selector: string) => {
    const positions = await reader.locator(selector).evaluateAll(elements => elements.slice(0, 8).map(el => ({ y: el.getBoundingClientRect().y, height: el.getBoundingClientRect().height })))
    for (let i = 1; i < positions.length; i++) expect(positions[i].y - positions[i - 1].y).toBeCloseTo(19, 0)
    expect(positions[1].height).toBeCloseTo(19, 0)
  }
  await checkLines('pre code .line')
  const strip = (await panel.locator('[data-panel-navigation]').boundingBox())!
  const toolbar = (await reader.locator('header').boundingBox())!
  expect(strip.height).toBeLessThanOrEqual(38)
  expect(toolbar.y + toolbar.height - strip.y).toBeLessThanOrEqual(77)
  await page.screenshot({ path: info.outputPath('compact-file-preview.png') })
  await panel.getByRole('button', { name: /^plain\.txt/ }).click()
  await expect(reader.locator('pre > span')).toHaveCount(83)
  await checkLines('pre > span')
  await reader.getByRole('button', { name: '关闭自动换行', exact: true }).click()
  await checkLines('pre > span')
  await noOverflow(page)
  expect(errors).toEqual([])
})


test('长文档可连续滚动到末页，只绘制附近页面，缩放保留阅读位置', async ({ page }, info) => {
  const { panel, errors } = await setup(page, false, undefined, ['long.docx'])
  await page.route('**/api/data-center/artifacts/preview?**', route => route.fulfill({ json: {
    kind: 'document', url: '/api/data-center/artifacts/content?path=long.docx', size_bytes: 1000,
  } }))
  await page.route('**/api/data-center/artifacts/content?**', route => route.fulfill({ contentType: 'application/pdf',
    body: readFileSync(new URL('./fixtures/panel-long-report.pdf', import.meta.url)),
  }))
  await panel.getByRole('button', { name: '文件', exact: true }).click()
  await panel.getByRole('button', { name: /^long\.docx/ }).click()
  await panel.getByRole('button', { name: '收起文件树', exact: true }).click()
  const viewport = panel.getByRole('region', { name: '连续文档页面', exact: true })
  await expect(viewport.locator('[data-pdf-page]')).toHaveCount(34)
  await expect(viewport.getByLabel('文档第 1 页', { exact: true })).toHaveAttribute('data-rendered', 'true')
  await viewport.hover()
  await page.mouse.wheel(0, 900)
  const currentPage = async () => Number((await panel.getByRole('toolbar', { name: '文档阅读工具' }).innerText()).match(/(\d+) \/ 34/)?.[1])
  await expect.poll(currentPage).toBeGreaterThan(1)
  const beforeZoom = await currentPage()
  await panel.getByRole('button', { name: '放大文档', exact: true }).click()
  await expect.poll(currentPage).toBeGreaterThanOrEqual(beforeZoom - 1)
  expect(await viewport.locator('canvas').count()).toBeLessThan(10)
  await viewport.evaluate(el => { el.scrollTop = el.scrollHeight })
  await expect(viewport.getByLabel('文档第 34 页', { exact: true })).toHaveAttribute('data-rendered', 'true')
  await expect(viewport.getByLabel('文档第 34 页', { exact: true })).toBeInViewport()
  await expect.poll(currentPage).toBe(34)
  expect(await viewport.locator('canvas').count()).toBeLessThan(10)
  await page.screenshot({ path: info.outputPath('continuous-document.png') })
  await noOverflow(page)
  expect(errors).toEqual([])
})
