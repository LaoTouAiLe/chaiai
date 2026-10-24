import { expect, test, type Page } from '@playwright/test'
import type { components } from '../src/api/schema.gen'

type Snapshot = components['schemas']['SessionSnapshot']
const workspace = { workspace_id: 'w1', display_name: '交互项目' }
const base: Snapshot = {
  schema_version: 1, session_id: 's1', snapshot_id: 'cp1', title: '第一问', mode: 'chat',
  branch_id: 'main', branch_title: '原讨论', workspace, session_state: 'completed',
  created_at: '2026-10-13T00:00:00Z', answer_streaming: false,
  messages: [{ kind: 'user', message_id: 'u1', text: '第一问' },
    { kind: 'assistant', message_id: 'a1', text: '第一问回答', complete: true, chunk_sequence: 0,
      reasoning_truncated: false, stream_channel: 'text' }],
}
const tree: components['schemas']['ConversationMapView'] = {
  session_id: 's1', active_branch_id: 'main', matched_turn_ids: [],
  branches: [{ branch_id: 'main', title: '原讨论', position: 0, active: true, tip_turn_id: 'u1' }],
  nodes: [{ turn_id: 'u1', parent_turn_id: null, origin_branch_id: 'main', ordinal: 1,
    question: '第一问', answer_preview: '第一问回答', state: 'completed', can_continue: true }],
}

async function backend(page: Page, initial: Snapshot = base) {
  let snapshot = structuredClone(initial)
  let map = structuredClone(tree)
  const submissions: Record<string, unknown>[] = []
  await page.route('**/api/**', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    if (url.pathname === '/api/health') return route.fulfill({ json: { model_configured: true } })
    if (url.pathname === '/api/workspaces') return route.fulfill({ json: [workspace] })
    if (url.pathname === '/api/tasks') return route.fulfill({ json: { tasks: [], pending_count: 0 } })
    if (url.pathname === '/api/model-connections') return route.fulfill({ json: [] })
    if (url.pathname === '/api/preferences') return route.fulfill({ status: 404, json: {} })
    if (url.pathname === '/api/sessions') return route.fulfill({ json: Array.from({ length: 45 }, (_, i) => ({
      session_id: `s${i + 1}`, title: i === 0 ? snapshot.title : `历史会话${i}`, workspace,
      mode: 'chat', is_active: i === 0, archived: false,
      created_at: base.created_at, updated_at: base.created_at,
    })) })
    if (url.pathname.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: ': ready\n\n' })
    if (url.pathname.endsWith('/conversation-map')) return route.fulfill({ json: map })
    if (url.pathname.endsWith('/turns/u1')) return route.fulfill({ json: base })
    if (url.pathname.endsWith('/messages') && req.method() === 'POST') {
      const body = req.postDataJSON() as Record<string, unknown>
      submissions.push(body)
      snapshot = { ...snapshot, snapshot_id: 'cp2', branch_id: 'b1', branch_title: '追问分支',
        messages: [...(base.messages ?? []), { kind: 'user', message_id: 'u2', text: String(body.text) },
          { kind: 'assistant', message_id: 'a2', text: '地图内的新回答', complete: true,
            chunk_sequence: 0, reasoning_truncated: false, stream_channel: 'text' }] }
      map = { ...map, active_branch_id: 'b1',
        branches: [...map.branches.map((branch) => ({ ...branch, active: false })),
          { branch_id: 'b1', title: '追问分支', active: true, position: 1, tip_turn_id: 'u2', fork_turn_id: 'u1' }],
        nodes: [...map.nodes, { turn_id: 'u2', parent_turn_id: 'u1', origin_branch_id: 'b1', ordinal: 2,
          question: String(body.text), answer_preview: '地图内的新回答', state: 'completed', can_continue: true }] }
      return route.fulfill({ json: { session_id: 's1', status: 'completed', snapshot_id: 'cp2', replayed: false } })
    }
    if (url.pathname === '/api/sessions/s1') return route.fulfill({ json: snapshot })
    return route.fulfill({ status: 404, json: {} })
  })
  await page.goto('/?live=1')
  await expect(page.getByRole('heading', { name: initial.title, exact: true })).toBeVisible()
  return submissions
}

test('地图追问在右侧提交并更新地图，关闭抽屉保留分支和草稿', async ({ page }, info) => {
  const submissions = await backend(page)
  await page.getByRole('tab', { name: '会话地图' }).click()
  await page.getByRole('button', { name: /第1轮 · 已完成/ }).click()
  await page.getByRole('button', { name: '基于此轮继续提问' }).click()
  const drawer = page.getByRole('complementary', { name: '地图追问' })
  await expect(drawer).toBeVisible()
  await expect(page.getByRole('tab', { name: '会话地图' })).toHaveAttribute('aria-selected', 'true')
  await drawer.getByRole('textbox', { name: '给智能体发消息' }).fill('从第一轮追问')
  await drawer.getByRole('button', { name: '发送消息' }).click()
  await expect(drawer.getByText('地图内的新回答', { exact: true })).toBeVisible()
  const copyIcon = drawer.getByRole('button', { name: '复制', exact: true }).first().locator('svg')
  expect((await copyIcon.boundingBox())!.width).toBe(Number(await copyIcon.getAttribute('width')))
  await expect(page.getByRole('button', { name: /第2轮 · 已完成/ })).toBeVisible()
  expect(submissions).toHaveLength(1)
  expect(submissions[0]).toMatchObject({ parent_turn_id: 'u1', expected_branch_id: 'main', expected_snapshot_id: 'cp1' })
  await page.getByRole('button', { name: '关闭分支提示' }).click()
  await expect(page.getByText(/当前分支：/)).toHaveCount(0)
  await drawer.getByRole('textbox', { name: '给智能体发消息' }).fill('保留草稿')
  await page.screenshot({ path: info.outputPath('map-followup.png') })
  await page.getByRole('button', { name: '关闭地图追问' }).click()
  await expect(drawer).toHaveCount(0)
  await expect(page.getByRole('combobox', { name: '当前分支' })).toHaveValue('b1')
  await page.getByRole('tab', { name: '对话', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toHaveValue('保留草稿')
})

test('长侧栏列表独立滚动，设置始终在视口底部', async ({ page }) => {
  await backend(page)
  if (await page.getByRole('button', { name: '展开侧边栏' }).isVisible()) await page.getByRole('button', { name: '展开侧边栏' }).click()
  const settings = page.getByRole('button', { name: '设置', exact: true })
  const before = await settings.boundingBox()
  await expect(page.getByText('历史会话6', { exact: true })).toHaveCount(0)
  await page.getByRole('button', { name: '交互项目展示更多会话' }).click()
  const scroll = page.getByRole('navigation', { name: '会话' }).locator('..')
  await scroll.evaluate((element) => { element.scrollTop = element.scrollHeight })
  await expect(settings).toBeInViewport()
  expect(await settings.boundingBox()).toEqual(before)
  await expect(page.getByText('历史会话44', { exact: true })).toBeInViewport()
  await page.getByRole('button', { name: '会话「历史会话44」的更多操作' }).click()
  await expect(page.getByRole('menuitem', { name: '归档会话' })).toBeInViewport({ ratio: 1 })
})

test('轨迹表头贴住滚动区域，上滑不会压缩顶部栏', async ({ page }, info) => {
  const messages: Snapshot['messages'] = Array.from({ length: 60 }, (_, i) => ({
    kind: 'tool' as const, message_id: `tm${i}`, tool: { tool_call_id: `t${i}`, tool_name: 'read_file',
      status: 'ok' as const, redacted_arguments: { path: `file${i}.txt` }, truncated: false,
      output_preview: `结果${i}`, elapsed_ms: 12 },
  }))
  await backend(page, { ...base, messages })
  await page.getByRole('tab', { name: '轨迹', exact: true }).click()
  const header = page.getByRole('tablist', { name: '会话视图' })
  const before = await header.boundingBox()
  const region = page.getByRole('region', { name: '轨迹', exact: true })
  await region.evaluate((element) => { element.scrollTop = 700 })
  const pane = await region.boundingBox()
  const th = await page.getByRole('columnheader', { name: '操作', exact: true }).boundingBox()
  expect(Math.abs(th!.y - pane!.y)).toBeLessThanOrEqual(1)
  expect(await header.boundingBox()).toEqual(before)
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= window.innerHeight)).toBe(true)
  await page.screenshot({ path: info.outputPath('trace-scroll.png') })
})

test('工具错误可关闭和重新查看，切换页签保持关闭状态', async ({ page }, info) => {
  const error: components['schemas']['ErrorView'] = {
    code: 'TOOL_FAILED', diagnostic_id: 'ui-error-1', category: 'tool', severity: 'error',
    title: '工具执行失败', message: '未能读取目标文件。', hint: '确认文件路径后重新提问。',
    retryable: false, occurred_at: base.created_at,
  }
  await backend(page, { ...base, messages: [...(base.messages ?? []), { kind: 'error', message_id: 'e1', error }] })
  await expect(page.getByRole('heading', { name: '工具执行失败' })).toBeVisible()
  await page.getByRole('button', { name: '关闭错误提示：工具执行失败' }).click()
  await page.getByRole('tab', { name: '轨迹', exact: true }).click()
  await page.getByRole('tab', { name: '对话', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工具执行失败' })).toHaveCount(0)
  await page.getByRole('button', { name: '查看已关闭的错误：工具执行失败' }).click()
  await expect(page.getByText(error.message, { exact: true })).toBeVisible()
  await page.getByText('诊断信息', { exact: true }).click()
  await expect(page.getByText('TOOL_FAILED · ui-error-1', { exact: true })).toBeVisible()
  await page.screenshot({ path: info.outputPath('dismissible-error.png') })
})
