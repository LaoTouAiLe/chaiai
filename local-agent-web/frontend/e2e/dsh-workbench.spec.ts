import { preferencesFixture } from './fixtures/preferences'
import { expect, test, type Page } from '@playwright/test'
import type { components } from '../src/api/schema.gen'

const workspace = { workspace_id: 'dsh-workspace', display_name: '界面验收项目' }
const snapshot: components['schemas']['SessionSnapshot'] = {
  schema_version: 1, session_id: 'dsh-session', snapshot_id: 'dsh-cp1', title: '界面验收会话',
  mode: 'chat', branch_id: 'main', branch_title: '原讨论', workspace, session_state: 'completed',
  created_at: '2026-11-02T00:00:00Z', answer_streaming: false,
  messages: [
    { kind: 'user', message_id: 'u1', text: '检查布局' },
    { kind: 'assistant', message_id: 'a1', text: '可以开始。', complete: true,
      chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text' },
  ],
}

async function liveShell(page: Page, initial: components['schemas']['SessionSnapshot'] = snapshot) {
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/health') return route.fulfill({ json: { model_configured: true } })
    if (url.pathname === '/api/workspaces') return route.fulfill({ json: [workspace] })
    if (url.pathname === '/api/tasks') return route.fulfill({ json: { tasks: [], pending_count: 0 } })
    if (url.pathname === '/api/model-connections') return route.fulfill({ json: [] })
    if (url.pathname === '/api/sessions') return route.fulfill({ json: [{
      session_id: snapshot.session_id, title: snapshot.title, workspace,
      mode: 'chat', is_active: true, archived: false,
      created_at: snapshot.created_at, updated_at: snapshot.created_at,
    }] })
    if (url.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: ': ready\n\n' })
    if (url.pathname === '/api/sessions/dsh-session') return route.fulfill({ json: initial })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/')
  await expect(page.getByRole('heading', { name: snapshot.title, exact: true })).toBeVisible()
}

test('AI prose keeps paragraph rhythm, semantic lists, readable code and local scrolling', async ({ page }, info) => {
  const code = 'const path = "' + 'workspace/'.repeat(36) + 'result"'
  const text = [
    '## 当前页面的改造建议', '',
    '这是一段 AI 回复正文，用于比较段落、标题与列表的阅读节奏。先确认信息层级，再检查显示效果。', '',
    '这是第二段正文，段落之间需要有清楚的留白。', '',
    '## 实施顺序', '',
    '- 先处理 **正文排版**。', '- 再整理工具过程。', '  - 保留原有记录。', '',
    '1. 检查段落间距。', '2. 检查列表序号。', '',
    '- [x] 检查已完成', '- [ ] 核对待办事项', '',
    '> 引用第一段。', '>', '> 引用第二段。', '',
    '### 代码与表格', '', '路径是 `src/app.ts`。', '',
    '```ts', code, '```', '',
    '| 模块 | 作用 | 状态 | 验证方式 |', '| --- | --- | --- | --- |',
    '| 会话正文 | 展示模型回答 | 已优化 | 检查标题、列表和段落 |', '',
    '最后核对实际窗口尺寸下的显示。',
  ].join('\n')
  const reasoning = Array.from({ length: 18 }, (_, index) => `第 ${index + 1} 段思路，先核对信息，再进行展示。`).join('\n\n')
  await liveShell(page, { ...snapshot, messages: [snapshot.messages![0]!, {
    kind: 'assistant', message_id: 'prose-answer', text, complete: true,
    chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text', reasoning,
  }] })
  const content = page.locator('[data-quotable]').first()
  await expect(content.getByRole('heading', { name: '当前页面的改造建议' })).toBeVisible()
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
    const actual = await content.evaluate(el => {
      const root = el.firstElementChild!
      const paragraphs = root.querySelectorAll(':scope > p')
      const headings = root.querySelectorAll('h2')
      const first = paragraphs[0]!.getBoundingClientRect()
      const second = paragraphs[1]!.getBoundingClientRect()
      const last = paragraphs[paragraphs.length - 1]!
      const pre = root.querySelector('pre')!
      return {
        font: getComputedStyle(paragraphs[0]!).fontSize,
        leading: getComputedStyle(paragraphs[0]!).lineHeight,
        paragraphGap: second.top - first.bottom,
        headingTop: getComputedStyle(headings[1]!).marginTop,
        headingBottom: getComputedStyle(headings[1]!).marginBottom,
        bullet: getComputedStyle(root.querySelector('ul')!).listStyleType,
        ordered: getComputedStyle(root.querySelector('ol')!).listStyleType,
        codeFont: getComputedStyle(pre).fontSize,
        codeLeading: getComputedStyle(pre).lineHeight,
        codeScrolls: pre.scrollWidth > pre.clientWidth,
        tailMargin: getComputedStyle(last).marginBottom,
        documentOverflows: document.documentElement.scrollWidth > innerWidth,
      }
    })
    expect(actual).toMatchObject({ font: '16px', leading: '28px', paragraphGap: 16,
      headingTop: '32px', headingBottom: '16px', bullet: 'disc', ordered: 'decimal',
      codeFont: '13px', codeLeading: '22px', codeScrolls: true, tailMargin: '0px', documentOverflows: false })
    await page.screenshot({ path: info.outputPath(`ai-prose-${theme}.png`) })
  }
  for (const [size, expected] of [['small', '14px'], ['large', '18px']]) {
    await page.evaluate(size => { document.documentElement.dataset.fontSize = size }, size)
    expect(await content.locator('p').first().evaluate(el => getComputedStyle(el).fontSize)).toBe(expected)
  }
  await page.evaluate(() => { document.documentElement.dataset.fontSize = 'normal' })
  await page.getByRole('button', { name: '思考', exact: true }).click()
  const thought = page.getByRole('region', { name: '思考内容', exact: true })
  const thoughtStyle = await thought.evaluate(el => ({ maxHeight: getComputedStyle(el).maxHeight, overflow: getComputedStyle(el).overflowY }))
  expect(thoughtStyle).toEqual({ maxHeight: 'none', overflow: 'visible' })
  expect(await thought.locator('p').count()).toBe(18)
  await content.getByRole('heading', { name: '当前页面的改造建议' }).scrollIntoViewIfNeeded()
  await expect(content.getByRole('heading', { name: '当前页面的改造建议' })).toBeInViewport()
})

test('completed tool work folds without hiding the answer and reply time is shown once', async ({ page }, info) => {
  await page.route('**/api/**', route => route.fulfill({ status: 404, json: {} }))
  await page.goto('/?state=shell')
  const process = page.getByRole('button', { name: /处理过程（/ })
  await expect(process).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByText('我先读现有 README 的目录结构，再决定插在哪一节后面。', { exact: true })).not.toBeVisible()
  await expect(page.getByText('已在「快速开始」后插入三步启动说明，并保留了原有的依赖前提描述。', { exact: true })).toBeVisible()
  await expect(page.getByLabel('回复时间', { exact: true })).toHaveCount(1)
  await expect(page.getByLabel('回复时间', { exact: true })).toHaveText('09:16')
  await page.screenshot({ path: info.outputPath('ai-answer-process-folded.png') })
  await process.click()
  await expect(page.getByText('读取文件', { exact: true })).toBeVisible()
  await expect(page.getByText('修改文件', { exact: true })).toBeVisible()
  await process.click()
  await expect(page.getByText('读取文件', { exact: true })).not.toBeVisible()
})

test('session rows stay compact while active work remains visible', async ({ page }, info) => {
  await page.route('**/api/**', route => route.fulfill({ status: 404, json: {} }))
  if (page.viewportSize()!.width <= 1024) {
    // The fixture shell is intentionally inert, so inspect the expanded version at a desktop width.
    await page.setViewportSize({ width: 1100, height: page.viewportSize()!.height })
  }
  await page.goto('/?state=shell')
  const sidebar = page.getByRole('complementary', { name: '会话与工作区' })
  const history = sidebar.getByRole('navigation', { name: '会话', exact: true })
  const completed = history.getByRole('button', { name: /给 README 补一段快速开始/ })
  await expect(completed).toBeVisible()
  expect((await completed.boundingBox())!.height).toBeLessThanOrEqual(40)
  await expect(completed).toHaveAttribute('title', /已完成.*刚刚/)
  await expect(sidebar.getByText('任务已结束', { exact: true })).toHaveCount(0)
  await expect(sidebar.getByText('本地会话', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: info.outputPath('compact-session-rows.png') })
  await page.goto('/?state=queue')
  const active = sidebar.locator('[data-active="true"]').first()
  await expect(active).toBeVisible()
  expect((await active.boundingBox())!.height).toBeGreaterThan(40)
  await expect(active.getByText('正在执行', { exact: true })).toBeVisible()
})

test('composer prioritizes model and send while context details remain accessible', async ({ page }, info) => {
  await liveShell(page)
  const composer = page.locator('[data-composer]')
  const mode = composer.getByRole('group', { name: '选择会话模式', exact: true })
  const model = composer.getByRole('button', { name: '选择会话模型', exact: true })
  const send = composer.getByRole('button', { name: '发送消息', exact: true })
  const attach = composer.getByRole('button', { name: '添加附件', exact: true })
  const meter = composer.getByRole('button', { name: '展开上下文详情', exact: true })
  await expect(model).toBeInViewport()
  await expect(send).toBeInViewport()
  await expect(attach).toBeInViewport()
  await expect(meter).toBeInViewport()
  await expect(composer.getByText('未流式', { exact: true })).toBeVisible()
  const controls = await Promise.all([mode, model, attach, meter, send].map(item => item.boundingBox()))
  for (const bounds of controls) {
    expect(bounds!.x).toBeGreaterThanOrEqual(0)
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width)
  }
  expect(controls[1]!.width).toBeGreaterThanOrEqual(60)
  expect(await model.locator('span').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  for (let index = 1; index < controls.length; index++) {
    const previous = controls[index - 1]!
    const next = controls[index]!
    expect(previous.y + previous.height <= next.y + 1 || previous.x + previous.width <= next.x + 1).toBe(true)
  }
  const meterWrapper = meter.locator('..')
  await expect(meterWrapper.locator('> span')).not.toBeVisible()
  await model.click()
  await expect(page.getByRole('dialog', { name: '模型选择', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '关闭模型选择', exact: true }).click()
  await meter.click()
  await expect(page.getByRole('dialog', { name: '上下文详情', exact: true })).toBeVisible()
  await expect(page.getByRole('region', { name: '上下文详情', exact: true }).getByText('占用', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: '上下文详情', exact: true })).toHaveCount(0)
  await expect(meter).toBeFocused()
  const help = page.getByRole('tooltip')
  await expect(help).toBeVisible()
  const helpBounds = (await help.boundingBox())!
  expect(helpBounds.x).toBeGreaterThanOrEqual((await page.locator('main').boundingBox())!.x)
  expect(helpBounds.x + helpBounds.width).toBeLessThanOrEqual(page.viewportSize()!.width)
  await composer.getByRole('textbox', { name: '给智能体发消息', exact: true }).click()
  await expect(help).not.toBeVisible()
  await page.screenshot({ path: info.outputPath('composer-primary-controls.png') })
  // A side-chat sized column uses the composer's own width, rather than the browser width.
  await composer.evaluate(el => { (el.firstElementChild as HTMLElement).style.width = '240px' })
  await expect(model).toBeInViewport()
  await expect(send).toBeInViewport()
  await expect.poll(async () => (await model.boundingBox())!.y - ((await mode.boundingBox())!.y + (await mode.boundingBox())!.height)).toBeGreaterThanOrEqual(0)
  const smallModel = (await model.boundingBox())!
  const smallSend = (await send.boundingBox())!
  expect(smallModel.x + smallModel.width).toBeLessThan(smallSend.x)
  await meter.focus()
  await expect(help).toBeVisible()
  expect((await help.boundingBox())!.x).toBeGreaterThanOrEqual((await page.locator('main').boundingBox())!.x)
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
})

test('comfortable reading width, workspace scale and image galleries adapt to the actual column', async ({ page }, info) => {
  await liveShell(page)
  let current = { ...snapshot, messages: [
    { kind: 'user', message_id: 'gallery-user', text: '看看这张工作区布局参考。', attachments: ['layout.png'] },
    { kind: 'assistant', message_id: 'gallery-answer', text: '## 留出阅读的空间\n\n正文、图片和输入框应当各有清晰的边界。字体更易读，左右留白也随窗口变化。\n\n- 工作区与会话保持一致的文字尺度。\n- 图片按比例预览，点击可以查看原图。', complete: true, chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text' },
  ] } satisfies components['schemas']['SessionSnapshot']
  await page.route('**/api/sessions/dsh-session', route => route.fulfill({ json: current }))
  await page.route('**/api/sessions/dsh-session/attachments?**', route => route.fulfill({ contentType: 'image/svg+xml', body:
    '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800"><rect width="1200" height="800" rx="48" fill="#e8edf7"/><rect x="72" y="72" width="240" height="656" rx="24" fill="#d1dbed"/><rect x="352" y="72" width="776" height="456" rx="24" fill="white"/><rect x="352" y="568" width="776" height="160" rx="32" fill="white"/><text x="400" y="260" font-family="sans-serif" font-size="50" fill="#42546e">A space to think.</text></svg>',
  }))
  await page.reload()
  const content = page.locator('[data-quotable]').first()
  await expect(content).toBeVisible()
  const single = page.getByRole('button', { name: '放大查看这张图片', exact: true })
  await expect.poll(async () => single.evaluate(el => el.getBoundingClientRect().height)).toBe(160)
  expect((await single.boundingBox())!.width).toBe(240)
  const assertAxis = async () => {
    const edges = await content.evaluate(el => {
      const prose = el.querySelector('p')!.getBoundingClientRect()
      const input = document.querySelector('[data-composer] textarea')!
      const card = input.closest('label')!.parentElement!.getBoundingClientRect()
      return { left: prose.left - card.left, right: card.right - prose.right, width: prose.width }
    })
    expect(edges.left).toBeCloseTo(16, 0)
    expect(edges.right).toBeCloseTo(16, 0)
    expect(edges.width).toBeLessThanOrEqual(920)
  }
  await assertAxis()
  const initialViewport = page.viewportSize()!
  if (initialViewport.width === 1440) {
    await page.setViewportSize({ width: 1910, height: 1000 })
    await assertAxis()
    expect(await content.locator('p').first().evaluate(el => el.getBoundingClientRect().width)).toBe(920)
    await page.setViewportSize(initialViewport)
  } else if (initialViewport.width === 390) {
    await page.setViewportSize({ width: 320, height: 844 })
    await assertAxis()
    const smallImage = (await single.boundingBox())!
    expect(smallImage.width).toBeLessThanOrEqual(240)
    expect(smallImage.width / smallImage.height).toBeCloseTo(1.5, 1)
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
    await page.setViewportSize(initialViewport)
  }
  await single.click()
  await expect(page.getByRole('dialog', { name: '放大查看图片' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(single).toBeFocused()
  if (page.viewportSize()!.width > 1024) {
    const workspaceLabel = page.getByRole('button', { name: workspace.display_name, exact: true })
    expect(await workspaceLabel.evaluate(el => getComputedStyle(el).fontSize)).toBe('15px')
    await page.getByRole('button', { name: '工作面板', exact: true }).click()
    await expect(page.getByRole('complementary', { name: '工作面板', exact: true })).toBeVisible()
    await assertAxis()
    await page.getByRole('button', { name: '工作面板', exact: true }).click()
  }
  await page.screenshot({ path: info.outputPath('comfortable-workbench.png') })
  await page.evaluate(() => { document.documentElement.dataset.fontSize = 'large' })
  expect(await content.locator('p').first().evaluate(el => getComputedStyle(el).fontSize)).toBe('18px')
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
  current = { ...current, messages: current.messages.map(message => message.kind === 'user' ? { ...message, attachments: ['layout.png', 'second.png'] } : message) }
  await page.reload()
  const tiles = page.getByRole('button', { name: '放大查看这张图片', exact: true })
  await expect(tiles).toHaveCount(2)
  for (const tile of await tiles.all()) {
    expect((await tile.boundingBox())!.width).toBe(64)
    expect((await tile.boundingBox())!.height).toBe(64)
  }
  await page.screenshot({ path: info.outputPath('comfortable-image-gallery.png') })
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
})

test('DSH tokens, light/dark surfaces and conversation controls fit each viewport', async ({ page }, info) => {
  await page.route('**/api/**', route => route.fulfill({ status: 404, json: {} }))
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  for (const theme of ['light', 'dark']) {
    for (const scene of ['empty-session', 'shell', 'queue', 'question', 'settings-models', 'model-menu']) {
      await page.goto(`/?state=${scene}`)
      await expect(page.locator('[data-scenario]')).toHaveAttribute('data-scenario', scene)
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme }, theme)
      const styles = await page.evaluate(() => {
        const root = getComputedStyle(document.documentElement)
        const main = getComputedStyle(document.querySelector('main')!)
        const probe = document.createElement('span')
        probe.style.color = 'var(--dsw-alias-bg-base)'
        document.body.appendChild(probe)
        const expected = getComputedStyle(probe).color
        probe.remove()
        const input = document.querySelector('[data-composer] textarea')
        const card = input?.closest('label')?.parentElement
        return {
          sidebar: root.getPropertyValue('--law-sidebar-width').trim(),
          font: getComputedStyle(document.body).fontFamily,
          background: main.backgroundColor,
          expected,
          overflow: document.documentElement.scrollWidth > innerWidth,
          shadow: card ? getComputedStyle(card).boxShadow : null,
        }
      })
      expect(styles.sidebar).toBe('272px')
      expect(styles.font).toContain('Segoe UI')
      expect(styles.background).toBe(styles.expected)
      expect(styles.overflow).toBe(false)
      if (styles.shadow !== null) expect(styles.shadow).not.toBe('none')
      if (!['question', 'settings-models'].includes(scene)) {
        await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toBeInViewport()
      }
      if (scene === 'settings-models') {
        // No vertically stacked Chinese characters caused by flex shrinking action buttons.
        const buttons = page.getByRole('dialog').getByRole('button', { name: /^(编辑|测试连接|设为默认|删除)$/ })
        for (const button of await buttons.all()) {
          expect(await button.evaluate(el => {
            const range = document.createRange()
            range.selectNodeContents(el)
            return range.getBoundingClientRect().height <= el.getBoundingClientRect().height
          })).toBe(true)
        }
      }
      if (['shell', 'empty-session', 'settings-models'].includes(scene)) {
        await page.screenshot({ path: info.outputPath(`${theme}-${scene}.png`) })
      }
    }
  }
  expect(errors).toEqual([])
})

test('app rail and session column collapse or open as a drawer without losing the draft', async ({ page }, info) => {
  await liveShell(page)
  const narrow = page.viewportSize()!.width <= 1024
  const sidebar = page.getByRole('complementary', { name: '会话与工作区' })
  const draft = page.getByRole('textbox', { name: '给智能体发消息' })
  await draft.fill('切换导航后保留这段草稿')
  if (!narrow) {
    const history = page.getByRole('navigation', { name: '会话', exact: true })
    const bounds = (await history.boundingBox())!
    expect(bounds.y).toBeLessThan(220)
    const historyArea = (await history.locator('..').boundingBox())!
    expect(historyArea.height).toBeGreaterThan(page.viewportSize()!.height * 0.7)
    const task = (await page.getByRole('button', { name: '任务中心', exact: true }).boundingBox())!
    expect(task.x + task.width).toBeLessThanOrEqual(48)
  }
  await page.getByRole('button', { name: narrow ? '展开侧边栏' : '收起侧边栏', exact: true }).click()
  await expect(sidebar).toHaveAttribute('data-collapsed', narrow ? 'false' : 'true')
  if (narrow) {
    await expect(page.getByRole('button', { name: '关闭导航遮罩' })).toBeVisible()
    await page.screenshot({ path: info.outputPath('navigation-drawer.png') })
    await page.getByRole('button', { name: '关闭导航遮罩' }).click({ position: { x: page.viewportSize()!.width - 12, y: 100 } })
    await expect(sidebar).toHaveAttribute('data-collapsed', 'true')
    await page.getByRole('button', { name: '展开侧边栏', exact: true }).click()
    await page.keyboard.press('Escape')
    await expect(sidebar).toHaveAttribute('data-collapsed', 'true')
  } else {
    expect((await sidebar.boundingBox())!.width).toBe(48)
    await page.getByRole('button', { name: '展开侧边栏', exact: true }).click()
    expect((await sidebar.boundingBox())!.width).toBe(320)
  }
  await expect(draft).toHaveValue('切换导航后保留这段草稿')
  if (narrow) await page.getByRole('button', { name: '展开侧边栏', exact: true }).click()
  const history = page.getByRole('navigation', { name: '会话', exact: true })
  const originalTop = (await history.boundingBox())!.y
  await page.getByRole('button', { name: '筛选会话', exact: true }).click()
  const filters = page.getByRole('dialog', { name: '会话筛选', exact: true })
  await expect(filters).toBeVisible()
  expect((await history.boundingBox())!.y).toBe(originalTop)
  await page.keyboard.press('Escape')
  await expect(filters).toHaveCount(0)
  await expect(sidebar).toHaveAttribute('data-collapsed', 'false')
  await expect(page.getByRole('button', { name: '筛选会话', exact: true })).toBeFocused()
  await page.getByRole('button', { name: '筛选会话', exact: true }).click()
  await filters.getByRole('button', { name: '进行中的会话', exact: true }).click()
  await expect(filters).toHaveCount(0)
  await expect(page.getByRole('button', { name: '筛选会话', exact: true })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: '筛选会话', exact: true }).click()
  await filters.getByRole('button', { name: '全部会话', exact: true }).click()
  await page.screenshot({ path: info.outputPath('compact-session-sidebar.png') })
  if (narrow) await page.getByRole('button', { name: '收起侧边栏', exact: true }).click()
  await page.getByRole('button', { name: '任务中心', exact: true }).click()
  await expect(page.getByRole('heading', { name: '任务中心', exact: true })).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.getByRole('button', { name: '会话工作台', exact: true }).click()
  await expect(draft).toHaveValue('切换导航后保留这段草稿')
})

test('settings and nested editor fit the viewport and restore keyboard focus', async ({ page }, info) => {
  await liveShell(page)
  await page.getByRole('button', { name: '设置', exact: true }).click()
  const settings = page.getByRole('dialog', { name: '设置', exact: true })
  await expect(settings).toBeInViewport()
  const add = settings.getByRole('button', { name: '添加连接', exact: true })
  await add.click()
  const editor = page.locator('[data-editor-dialog]')
  await expect(editor).toBeInViewport()
  const bounds = (await editor.boundingBox())!
  expect(bounds.x).toBeGreaterThanOrEqual(0)
  expect(bounds.y).toBeGreaterThanOrEqual(0)
  expect(bounds.x + bounds.width).toBeLessThanOrEqual(page.viewportSize()!.width)
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(page.viewportSize()!.height)
  await page.screenshot({ path: info.outputPath('connection-editor.png') })
  await page.keyboard.press('Escape')
  await expect(editor).toHaveCount(0)
  await expect(settings).toBeVisible()
  await expect(add).toBeFocused()
})


test('shared DSH typography, corner scale and focus remain consistent', async ({ page }, info) => {
  await liveShell(page, { ...snapshot, messages: [
    { kind: 'user', message_id: 'style-user', text: '查看界面与代码' },
    { kind: 'assistant', message_id: 'style-ai', text: '## 一起完成工作\n\n正文使用统一的字体与段落节奏。\n\n```ts\nconst title = "中文也使用清晰的字体"\n```', complete: true, chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text' },
  ] })
  const metrics = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement)
    const body = getComputedStyle(document.body)
    const code = getComputedStyle(document.querySelector('[role="log"] code')!)
    const composer = getComputedStyle(document.querySelector('[data-composer] textarea')!.closest('label')!.parentElement!)
    return { uiFont: body.fontFamily, monoFont: code.fontFamily, radius: composer.borderRadius,
      control: root.getPropertyValue('--r-control').trim(), overlay: root.getPropertyValue('--r-overlay').trim() }
  })
  expect(metrics.uiFont).toContain('Microsoft YaHei')
  expect(metrics.monoFont).toContain('Microsoft YaHei')
  expect(metrics.monoFont).not.toMatch(/,\s*monospace$/)
  expect(metrics.radius).toBe(page.viewportSize()!.width <= 600 ? '24px' : '28px')
  expect(metrics.control).toBe('12px')
  expect(metrics.overlay).toBe('28px')
  await page.getByRole('textbox', { name: '给智能体发消息', exact: true }).focus()
  await expect(page.getByRole('textbox', { name: '给智能体发消息', exact: true })).toBeFocused()
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
  await page.screenshot({ path: info.outputPath('dsh-whole-workbench.png') })
})


test('workspace folds survive refresh and search reveals matches without changing the active chat', async ({ page }, info) => {
  await liveShell(page)
  const sidebar = page.getByRole('complementary', { name: '会话与工作区' })
  const revealSidebar = async () => {
    const expand = sidebar.getByRole('button', { name: '展开侧边栏', exact: true })
    if (await expand.isVisible()) await expand.click()
  }
  await revealSidebar()
  const group = sidebar.getByRole('button', { name: '收起工作区「界面验收项目」', exact: true })
  await group.focus()
  await page.keyboard.press('Enter')
  const collapsed = sidebar.getByRole('button', { name: '展开工作区「界面验收项目」', exact: true })
  await expect(collapsed).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('heading', { name: snapshot.title, exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: snapshot.title, exact: true })).toBeVisible()
  await revealSidebar()
  await expect(collapsed).toHaveAttribute('aria-expanded', 'false')
  const search = sidebar.getByRole('searchbox', { name: '搜索会话', exact: true })
  await search.fill(snapshot.title)
  await expect(group).toHaveAttribute('aria-expanded', 'true')
  await expect(group).toBeDisabled()
  await expect(sidebar.getByRole('button', { name: new RegExp(`^已完成 ${snapshot.title}`) })).toBeVisible()
  await search.fill('')
  await expect(collapsed).toHaveAttribute('aria-expanded', 'false')
  await collapsed.click()
  await expect(group).toHaveAttribute('aria-expanded', 'true')
  await page.screenshot({ path: info.outputPath('workspace-expansion.png') })
})


test('settings search, navigation, drafts and narrow category picker work together', async ({ page }, info) => {
  await liveShell(page)
  await page.route('**/api/preferences', route => route.fulfill({ json: preferencesFixture() }))
  await page.getByRole('button', { name: '设置', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: '设置', exact: true })
  const search = dialog.getByRole('searchbox', { name: '搜索设置项', exact: true })
  const choose = async (label: string, value: string) => {
    if (page.viewportSize()!.width <= 760) await dialog.getByRole('combobox', { name: '选择设置分类' }).selectOption(value)
    else await dialog.getByRole('tab', { name: label, exact: true }).click()
  }
  await search.fill('域名')
  await expect(dialog.getByRole('listbox', { name: '设置项搜索结果' })).toBeVisible()
  await choose('通用与外观', 'general')
  await expect(search).toHaveValue('')
  const general = dialog.getByRole('tabpanel', { name: '通用与外观' })
  await expect(general).toBeVisible()
  await general.getByRole('combobox', { name: '文字大小', exact: true }).selectOption('large')
  await expect(general.getByRole('status')).toContainText('尚未保存')
  await choose('模型连接', 'models')
  await choose('通用与外观', 'general')
  await expect(general.getByRole('combobox', { name: '文字大小', exact: true })).toHaveValue('large')
  await search.fill('域名')
  await search.press('Escape')
  await expect(search).toHaveValue('')
  await expect(dialog).toBeVisible()
  await search.fill('域名')
  await dialog.getByRole('button', { name: '清空设置搜索' }).click()
  await expect(search).toBeFocused()
  await expect(search).toHaveValue('')
  await search.fill('拒绝的域名')
  await dialog.getByRole('option', { name: /拒绝的域名/ }).click()
  const domain = dialog.getByRole('textbox', { name: '拒绝的域名', exact: true })
  await expect(domain).toBeFocused()
  await expect(domain).toBeInViewport()
  await choose('记忆巩固', 'consolidation')
  await expect(dialog.getByRole('tabpanel', { name: '记忆巩固' })).toBeVisible()
  await search.fill('启用后台记忆巩固')
  await dialog.getByRole('option', { name: /启用后台记忆巩固/ }).click()
  await expect(dialog.getByRole('checkbox', { name: '启用后台记忆巩固', exact: true })).toBeFocused()
  await choose('通用与外观', 'general')
  await expect(general.getByRole('combobox', { name: '文字大小', exact: true })).toHaveValue('large')
  await page.screenshot({ path: info.outputPath('settings-navigation.png') })
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false)
})
