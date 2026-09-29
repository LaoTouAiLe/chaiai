import { expect, test } from '@playwright/test'

/**
 * 审批端到端（T32）：真服务 + 真前端，假模型按输入发出一次**真的越过免审上限**的大范围编辑。
 *
 * 为什么这条必须打真服务：审批卡接管输入区、`approval_id` 的提交、结构化 diff 的可见性、
 * 以及"刷新之后挂起审批还在"——这四件事全都发生在**服务端的检查点**与前端之间。用 `page.route`
 * 打桩只能证明"页面会按我编的数据渲染"，证明不了"决定真的带着 approval id 提交了、而且结果
 * 以后端为准"，也证明不了"刷新之后权限还在后端手里"（T32 的两条停止条件）。
 *
 * 服务与输入由 `playwright.approval.config.ts` 拉起：
 * `scripts/serve_fake.py --decision tool --approval-edit` 会在工作区里种一份 `notes.txt`，
 * 并在收到 `T32_APPROVE` / `T32_REJECT` 时各发出一次"201 行替换成 1 行"的 `edit_file`
 * （改动 202 行 > 免审上限 200）——安全阈值仍然由产品代码裁决，端到端不放宽任何配置。
 */
const REPLY = '这是假模型的固定回答。'
const NEW_TEXT = '已批准的新内容'
/**
 * 拒绝之后那一轮的回答。
 *
 * 拒绝**不会**让图继续往下走：`nodes.REJECTED_REPLY` 直接作为最终回答，且不产生工具结果
 * （见 `tests/test_graph_resume.py::test_delete_rejection_keeps_file_and_stops` 里
 * `graph.get_state(...).next == ()`）。所以这里不能断言假模型那句固定回答——那句话属于
 * "工具执行完之后接着往下生成"的那条路径。
 */
const REJECTED_REPLY = '该动作已被你拒绝，我不会执行，也不会自动重试。'

/** 新建会话并提交一句话；返回本次会话的输入区 locator。 */
async function submit(page: import('@playwright/test').Page, text: string) {
  await page.getByRole('button', { name: '新建会话' }).click()
  const input = page.getByPlaceholder('给智能体发消息')
  await input.fill(text)
  await page.getByRole('button', { name: '发送消息' }).click()
  return input
}

/** 展开那次编辑的工具行（收起时只有一行摘要，结构化 diff 在展开后才在 DOM 里）。 */
async function expandEditRow(page: import('@playwright/test').Page) {
  const row = page.getByRole('button', { name: /edit_file/ })
  await expect(row).toHaveAttribute('aria-expanded', 'false')
  await row.click()
  await expect(row).toHaveAttribute('aria-expanded', 'true')
  return page.locator('figure').filter({ hasText: 'notes.txt' })
}

test('批准一次大范围编辑：卡片接管输入区，刷新不丢，批准后 diff 与工具结果一致', async ({ page }) => {
  await page.goto('/')
  const input = await submit(page, 'T32_APPROVE')

  // 1) 审批卡接管输入区：动作、规范化目标与参数都在，按钮可达。
  const card = page.getByLabel('审批')
  await expect(card.getByText('等待审批', { exact: true })).toBeVisible()
  await expect(card.getByText('edit_file')).toBeVisible()
  await expect(card.getByText('notes.txt', { exact: true }).first()).toBeVisible()
  await expect(card.getByText('path=notes.txt')).toBeVisible()
  await expect(input).toHaveCount(0)

  // 2) 刷新恢复：挂起审批来自服务端检查点，不靠浏览器里的本地状态。
  await page.reload()
  await expect(card.getByText('等待审批', { exact: true })).toBeVisible()
  const allow = card.getByRole('button', { name: '允许一次' })
  await expect(allow).toBeEnabled()

  // 3) 提交决定：卡片按后端结果转为"已批准"，输入区回来（不再是待决状态）。
  await allow.click()
  await expect(card.getByText('已批准', { exact: true })).toBeVisible()
  await expect(page.getByPlaceholder('给智能体发消息')).toBeVisible()

  // 4) 工具结果的结构化 diff：计数与新增行内容都来自服务端字段，页面不解析文本。
  const diff = await expandEditRow(page)
  await expect(diff.locator('figcaption')).toContainText('notes.txt')
  await expect(diff.locator('figcaption')).toContainText('+1')
  await expect(diff.locator('figcaption')).toContainText('−201')
  await expect(diff.getByText(NEW_TEXT)).toBeVisible()
  // 这一轮最终也有回答：改动、工具结果与最终回答是同一条链路上的三件事。
  await expect(page.getByText(REPLY)).toBeVisible()

  // 5) 刷新之后一切一致：diff、审批决定都还是服务端说的那一份。
  await page.reload()
  await expect(page.getByText(REPLY)).toBeVisible()
  await expect(page.getByLabel('审批').getByText('已批准', { exact: true })).toBeVisible()
  const afterReload = await expandEditRow(page)
  await expect(afterReload.getByText(NEW_TEXT)).toBeVisible()
  await expect(afterReload.locator('figcaption')).toContainText('−201')
})

test('拒绝一次大范围编辑：卡片如实说未执行，文件没有任何改动', async ({ page }) => {
  await page.goto('/')
  await submit(page, 'T32_REJECT')

  const card = page.getByLabel('审批')
  await expect(card.getByText('等待审批', { exact: true })).toBeVisible()
  await card.getByRole('button', { name: '拒绝' }).click()

  // 拒绝之后：卡片只报告结果，输入区恢复（用户可以立刻重新发起请求）。
  await expect(card.getByText('已拒绝', { exact: true })).toBeVisible()
  await expect(card.getByText('该操作未执行。')).toBeVisible()
  await expect(page.getByPlaceholder('给智能体发消息')).toBeVisible()
  await expect(card.getByRole('button', { name: '允许一次' })).toHaveCount(0)

  // 没有任何结构化 diff：这次调用没有落盘，页面也不该显示"改了什么"。
  await expect(page.locator('figure')).toHaveCount(0)
  // 拒绝不是"整轮失败"：图就此结束，并把"不会执行、也不会自动重试"如实写进回答。
  await expect(page.getByText(REJECTED_REPLY)).toBeVisible()
  await expect(page.getByText(REPLY)).toHaveCount(0)
})