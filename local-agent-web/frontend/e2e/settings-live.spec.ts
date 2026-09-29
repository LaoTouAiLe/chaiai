// T29A 端到端：`?live=1` 下的**真实**设置对话框（真浏览器、真组件、真表单）。
//
// 后端用 `page.route` 拦下来给确定性响应：这条用例要证的是"页面接上了没有"——
// 表单能提交、按钮真发请求、列表按响应更新；服务端行为由 Python 侧的测试负责。
// 因此它**不需要**起 Python 服务，也不会碰任何真实端点或密钥。

import { expect, test, type Page, type Route } from '@playwright/test'

const CONNECTION = {
  connection_id: 'mc-live',
  display_name: '局域网网关',
  base_url: 'https://api.example.com/v1',
  provider: 'openai_compatible',
  model_id: 'mid-model',
  context_window_tokens: 32000,
  max_tokens: 1000,
  temperature: 0.2,
  timeout_seconds: 120,
  retry_count: 2,
  streaming: false,
  auth_mode: 'api_key',
  key_status: 'configured',
  is_default: true,
  config_version: '1',
  status: 'unverified',
  transport: 'https',
  capabilities: [],
}

const HEALTH = {
  status: 'ok',
  schema_version: 3,
  model_configured: false,
  model_name: '',
  credential_store: 'os',
  credential_note: null,
  storage: 'sqlite',
  static_frontend: true,
}

/** 一个最小的内存后端：只实现这条用例走到的几条路由。 */
async function stubBackend(page: Page, options: { failDefaultOnce?: boolean } = {}) {
  const connections: Record<string, unknown>[] = []
  const seen: string[] = []
  let defaultAttempts = 0

  await page.route('**/api/**', async (route: Route) => {
    const request = route.request()
    const url = new URL(request.url())
    const key = `${request.method()} ${url.pathname}`
    seen.push(key)

    if (key === 'GET /api/health') {
      return route.fulfill({ json: { ...HEALTH, model_configured: connections.length > 0 } })
    }
    if (key === 'GET /api/model-connections') {
      return route.fulfill({ json: connections })
    }
    if (key === 'POST /api/model-connections') {
      connections.push({ ...CONNECTION, display_name: '新连接', is_default: false })
      return route.fulfill({ json: connections[0] })
    }
    if (key === 'PATCH /api/model-connections/mc-live') {
      // 编辑产生**新版本**：版本号必须递增（页面据此显示"从下一轮起生效"）。
      const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>
      const stored = (connections[0] ?? CONNECTION) as Record<string, unknown>
      const updated = {
        ...stored,
        display_name: String(body.display_name ?? stored.display_name),
        model_id: String(body.model ?? stored.model_id),
        config_version: String(Number(stored.config_version) + 1),
      }
      connections[0] = updated
      return route.fulfill({ json: updated })
    }
    if (key === 'POST /api/model-connections/mc-live/test') {
      // 返回**同一份**记录（只改检测结论）：响应不自洽的话，页面会拿一个别的名字把卡片换掉。
      const stored = connections[0] ?? CONNECTION
      return route.fulfill({ json: { ...stored, status: 'verified' } })
    }
    if (key === 'PUT /api/model-connections/default') {
      // 页面必须回传"我看到的当前选择"（§6.1）。桩在这里**硬性要求**它：
      // 前端要是没带，这条用例就会失败——而不是"看起来成功、实际覆盖了别人"。
      const body = JSON.parse(request.postData() ?? '{}') as Record<string, unknown>
      if (!('expected_selection' in body) || typeof body.expected_version !== 'number') {
        return route.fulfill({ status: 422, json: { detail: '缺选择观测或目标版本' } })
      }
      defaultAttempts += 1
      if (options.failDefaultOnce && defaultAttempts === 1) {
        return route.fulfill({
          status: 409,
          json: {
            error: {
              code: 'MODEL_SETTINGS_CONFLICT',
              message: '默认连接已被其他页面修改',
              hint: '刷新后重试',
              retryable: false,
            },
          },
        })
      }
      const stored = { ...(connections[0] ?? CONNECTION), is_default: true }
      connections[0] = stored
      return route.fulfill({ json: stored })
    }
    if (key === 'DELETE /api/model-connections/mc-live') {
      const stored = connections[0] ?? CONNECTION
      connections.length = 0
      return route.fulfill({ json: stored })
    }
    return route.fulfill({ status: 404, json: { detail: `未准备的请求：${key}` } })
  })

  return { connections, seen }
}

test('新增连接、主动测试与停用都能真的走通', async ({ page }) => {
  const backend = await stubBackend(page)

  await page.goto('/?state=settings-models&live=1')
  await expect(page.getByRole('dialog', { name: '设置' })).toBeVisible()
  // 初始为空：这时页面说的是"还没有模型连接"，而不是拿 fixture 充数。
  await expect(page.getByText('还没有模型连接')).toBeVisible()

  // 1) 新增：表单校验会先挡住明显不合法的输入（窗口必须大于输出上限）。
  await page.getByRole('button', { name: '添加连接' }).click()
  const form = page.getByRole('form', { name: '新增连接' })
  await form.getByLabel('名称').fill('新连接')
  await form.getByLabel('端点（base_url）').fill('https://api.example.com/v1')
  await form.getByLabel('模型 ID').fill('mid-model')
  await form.getByLabel('上下文窗口（token，必填）').fill('500')
  await form.getByLabel('输出上限（max_tokens）').fill('1000')
  await form.getByRole('button', { name: '保存并设为默认' }).click()
  await expect(form.getByRole('alert')).toContainText('必须大于输出上限')

  await form.getByLabel('上下文窗口（token，必填）').fill('32000')
  await form.getByRole('button', { name: '保存并设为默认' }).click()

  // 2) 列表按响应更新，并且"未验证"如实显示（不假装测过）。
  await expect(page.getByRole('article', { name: '连接 新连接' })).toBeVisible()
  await expect(page.getByRole('article', { name: '连接 新连接' })).toContainText('未验证')

  // 3) 编辑：打开的是**旧值**，保存后是**新版本**（复审要求这条端到端）。
  await page.getByRole('button', { name: '编辑' }).click()
  const editForm = page.getByRole('form', { name: '编辑连接' })
  await expect(editForm.getByLabel('名称')).toHaveValue('新连接')
  await expect(editForm.getByLabel('端点（base_url）')).toHaveValue('https://api.example.com/v1')
  await editForm.getByLabel('名称').fill('改过的名字')
  await editForm.getByRole('button', { name: '保存修改' }).click()

  await expect(page.getByRole('article', { name: '连接 改过的名字' })).toBeVisible()
  // 编辑只对下一轮生效：卡片要如实说明，而不是假装立刻换了。
  await expect(page.getByText('编辑已保存为新版本')).toBeVisible()

  // 4) 主动测试：按钮真的发了请求，回来后状态变成可信的"已验证"。
  await page.getByRole('button', { name: '测试连接' }).click()
  await expect(page.getByRole('article', { name: '连接 改过的名字' })).toContainText('已验证')

  // 4) 删除要二次确认，确认后行消失（停用 = 不再出现在列表里）。
  await page.getByRole('button', { name: '删除', exact: true }).click()
  await page.getByRole('button', { name: '确认删除' }).click()
  await expect(page.getByText('还没有模型连接')).toBeVisible()

  expect(backend.seen).toContain('GET /api/model-connections')
  expect(backend.seen).toContain('POST /api/model-connections/mc-live/test')
  expect(backend.seen).toContain('DELETE /api/model-connections/mc-live')
})

test('创建成功但设默认冲突后只重试选择，不重复创建', async ({ page }) => {
  const backend = await stubBackend(page, { failDefaultOnce: true })
  await page.goto('/?state=settings-models&live=1')
  await page.getByRole('button', { name: '添加连接' }).click()
  const form = page.getByRole('form', { name: '新增连接' })
  await form.getByLabel('名称').fill('新连接')
  await form.getByLabel('端点（base_url）').fill('https://api.example.com/v1')
  await form.getByLabel('模型 ID').fill('mid-model')
  await form.getByLabel('上下文窗口（token，必填）').fill('32000')
  await form.getByRole('button', { name: '保存并设为默认' }).click()

  await expect(page.getByRole('alert')).toContainText('连接已保存，但尚未设为默认')
  await expect(form).toHaveCount(0)
  await expect(page.getByRole('article', { name: '连接 新连接' })).toBeVisible()
  expect(backend.seen.filter((key) => key === 'POST /api/model-connections')).toHaveLength(1)

  await page.getByRole('button', { name: '刷新连接列表' }).click()
  await page.getByRole('button', { name: '设为默认' }).click()
  await expect(page.getByRole('article', { name: '连接 新连接' })).toContainText('默认')
  expect(backend.seen.filter((key) => key === 'POST /api/model-connections')).toHaveLength(1)
})
