import { expect, test, type Page } from '@playwright/test'

/**
 * Visual baseline for the T26 static shell.
 *
 * Each scenario is captured once per configured viewport (1440×900 and
 * 1280×800) by the projects in `playwright.config.ts`. Baselines are created
 * with `--update-snapshots` and may only be refreshed after a human has
 * confirmed the difference — never in bulk to silence a regression.
 */
const ALL_SCENARIOS = [
  'shell',
  'diff',
  'errors',
  'tools',
  'trace',
  'long-text',
  'approval',
  'approval-expired',
  'settings-models',
  'settings-empty',
  'model-menu',
  'memory-hit',
  'memory-miss',
  // v0.5 §10.4：写入被跳过（有候选但一条都没落）——第三种写入事实也要有像素证据。
  'memory-skipped',
  'memory-failed',
  'empty-no-workspace',
  'empty-session',
  'running-empty',
  'loading-history',
  'meter-open',
  'sidebar-collapsed',
  'error-codes',
  // v0.5 T56：能力与档位面板（四种不可用原因各一次）。新场景只新增 PNG，不改任何既有基线。
  'capabilities',
] as const

/**
 * Scenarios whose composer stays free and able to send.
 *
 * Excluded by design: a takeover replaces the composer (`approval`), the input is
 * disabled with no workspace (`empty-no-workspace`), and a running turn offers
 * stopping instead of sending (`running-empty`).
 */
const COMPOSER_SCENARIOS = [
  'shell',
  'diff',
  'errors',
  'tools',
  'empty-session',
  'loading-history',
  'memory-hit',
  'memory-miss',
  'memory-failed',
] as const

/**
 * Wait until the page has painted with its final text metrics.
 *
 * Without this the baseline was flaky under parallel workers: a screenshot could
 * land before `document.fonts.ready` resolved, so the same page produced two
 * different images. Two rAFs then let the post-swap layout commit before capture.
 */
async function settle(page: Page, scenario?: string): Promise<void> {
  if (scenario !== undefined) {
    await expect(page.locator('[data-scenario]')).toHaveAttribute('data-scenario', scenario)
  }
  await page.evaluate(async () => {
    await document.fonts.ready
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          resolve()
        })
      })
    })
  })
}

for (const scenario of ALL_SCENARIOS) {
  test(`prototype ${scenario}`, async ({ page }) => {
    await page.goto(`/?state=${scenario}`)
    await settle(page, scenario)

    // Layout guard: nothing may overflow the page horizontally at either viewport.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    )
    expect(overflow).toBeLessThanOrEqual(0)

    await expect(page).toHaveScreenshot(`${scenario}.png`)
  })
}

for (const scenario of COMPOSER_SCENARIOS) {
  test(`composer stays reachable in ${scenario}`, async ({ page }) => {
    await page.goto(`/?state=${scenario}`)
    await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toBeVisible()
    await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible()
  })
}

test('diff checkpoint shows both hunks with signs and the long line', async ({ page }) => {
  await page.goto('/?state=diff')
  // 原型 fixture 里这次编辑本身就是 `expanded: true`，diff 卡直接可见；曾经需要先点开的
  // "执行过程 · N 个动作"聚合行已在 Think 那一轮移除（工具行各自折叠，不跨助手正文搬运动作）。
  const card = page.locator('figure')
  await expect(card.getByText('src/local_agent/config.py')).toBeVisible()

  await expect(card.getByText(/^@@ -\d+/)).toHaveCount(2)
  await expect(card.getByText(/compact_long_tool_outputs_after_tokens: int = Field/)).toBeVisible()
  await expect(card.getByText('+5')).toBeVisible()
  await expect(card.getByText('−3')).toBeVisible()
})

test('error checkpoint shows all three provider failures with actionable retry rules', async ({ page }) => {
  await page.goto('/?state=errors')
  await expect(page.getByText('MODEL_AUTH_FAILED')).toHaveCount(1)
  await expect(page.getByText('MODEL_RATE_LIMITED')).toHaveCount(1)
  await expect(page.getByText('MODEL_RETRY_EXHAUSTED')).toHaveCount(1)
  await page.getByText('诊断信息').first().click()
  await expect(page.getByText('MODEL_AUTH_FAILED')).toBeVisible()

  // Retry is guidance, not a dead button that could imply a tool was rerun.
  await expect(page.getByText('检查后可在输入框重新发送')).toHaveCount(2)
  await expect(page.getByRole('button', { name: '重新发送' })).toHaveCount(0)
  await expect(page.getByText('需先处理配置（更新该连接的密钥），不能自动重试。')).toBeVisible()

  const text = await page.locator('body').innerText()
  expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
  expect(text).not.toMatch(/[A-Za-z]:\\/)
})

test('tool checkpoint renders every card shape, capped results included', async ({ page }) => {
  await page.goto('/?state=tools')
  // Two commands: one failing, one succeeding — both terminal cards are present.
  await expect(page.getByRole('region', { name: '终端输出' })).toHaveCount(2)
  await expect(page.getByRole('region', { name: '搜索结果' })).toHaveCount(2)
  await expect(page.getByRole('region', { name: '读取结果' })).toBeVisible()
  await expect(page.getByRole('region', { name: '任务清单' })).toBeVisible()

  await expect(page.getByText('退出码 1')).toBeVisible()
  await expect(page.getByText('退出码 0')).toBeAttached()
  await expect(page.getByText(/结果已截断，仅显示前 2 个文件/)).toBeVisible()
  await expect(page.getByText(/当前省略了 8 行/)).toBeVisible()
  // The unknown tool must land on the safe renderer, never a specialised card.
  await expect(page.getByText('结果详情', { exact: true })).toBeAttached()
})

test('an approval takes over the composer and a stale one offers no decision', async ({ page }) => {
  await page.goto('/?state=approval')
  await expect(page.getByRole('region', { name: '审批' })).toBeVisible()
  // 本场景 fixture 是 delete_file 高风险审批：首屏按钮是**二次确认的入口**，不是"允许一次"。
  // 普通风险仍直接给"允许一次"（真后端用例 `approval-live.spec.ts` 覆盖那一支）。
  await expect(page.getByRole('button', { name: '进入二次确认' })).toBeVisible()
  await expect(page.getByRole('button', { name: '拒绝' })).toBeVisible()
  // The chat input must not stay reachable under a pending decision.
  await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toHaveCount(0)

  await page.goto('/?state=approval-expired')
  await expect(page.getByText('审批已失效')).toBeVisible()
  await expect(page.getByRole('button', { name: '进入二次确认' })).toHaveCount(0)
  // A finished decision collapses to one line by default: the outcome stays on
  // screen without holding the whole space above the composer, and the detail is
  // one click away.
  await expect(page.getByText(/该审批已超时失效，未执行任何操作/)).toHaveCount(0)
  await page.getByRole('button', { name: '展开详情' }).click()
  await expect(page.getByText(/该审批已超时失效，未执行任何操作/)).toBeVisible()
  // `exact`：侧栏那枚"收起侧边栏"也含"收起"，子串匹配会撞上它。
  await expect(page.getByRole('button', { name: '收起', exact: true })).toBeVisible()
  // A card telling the user to re-issue a request must not be the reason they
  // cannot: the composer comes back while the outcome stays on screen.
  await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toBeVisible()
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible()
})

test('the composer stays keyboard reachable', async ({ page }) => {
  await page.goto('/?state=shell')
  const composer = page.getByRole('textbox', { name: '给智能体发消息' })
  await composer.click()
  await expect(composer).toBeFocused()

  // Typing must not shift the layout, and the send action must remain visible.
  await composer.fill('检查发送前的布局稳定性')
  await expect(page.getByRole('button', { name: '发送消息' })).toBeVisible()
})

/** Scroll the conversation pane (not the page) to its end. */
async function scrollConversationToEnd(page: Page): Promise<void> {
  await page.getByRole('log').evaluate((node) => {
    node.scrollTop = node.scrollHeight
  })
}

/**
 * Narrow desktop proof: the tail of a tall card must be reachable by scrolling.
 *
 * The first-pass 800px screenshot only showed that the tail was *below the fold*;
 * these tests show it becomes fully readable and operable after scrolling, which
 * is the claim the review asked to substantiate.
 */
test('diff tail is fully visible after scrolling to the end', async ({ page }, testInfo) => {
  // 窄窗里尾部卡片本身比视口高，`ratio: 1` 是几何上不可能满足的——那是窄窗的必然结果，
  // 不是布局缺陷。这条断言的意义只在宽窗成立。
  test.skip(testInfo.project.name === '640x800', '窄窗下元素高于视口')
  await page.goto('/?state=diff')
  // 与上一条同理：fixture 里这次编辑已展开，不需要（也再没有）"执行过程"聚合行可点。
  const card = page.locator('figure')
  await scrollConversationToEnd(page)
  await expect(card.getByText(/def validate_ratio\(cls\)/)).toBeInViewport({ ratio: 1 })
  await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toBeVisible()
  await settle(page)
  await expect(page).toHaveScreenshot('diff-bottom.png')
})

test('the third failure card is fully readable and operable after scrolling', async ({ page }) => {
  await page.goto('/?state=errors')
  const cards = page.getByRole('region', { name: '模型错误' })
  await expect(cards).toHaveCount(3)
  const third = cards.nth(2)
  await scrollConversationToEnd(page)

  // Full hint text and the optional diagnostic control must both be reachable.
  await expect(third.getByText(/检查该连接端点是否可达/)).toBeInViewport({ ratio: 1 })
  await expect(third.getByText('诊断信息')).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('errors-bottom.png')
})

/**
 * The shell must not contradict itself: the highlighted sidebar row and the
 * conversation header describe the same session. A header that disagrees with
 * the selection is a fixture bug, not a layout choice.
 */
test('every scenario keeps the header in sync with the selected sidebar row', async ({ page }) => {
  for (const scenario of ALL_SCENARIOS) {
    await page.goto(`/?state=${scenario}`)
    // Collapsed rail: the tree is not rendered, so there is no hidden-but-selected
    // row for the header to contradict.
    if (await page.locator('aside[data-collapsed="true"]').count() > 0) {
      await expect(page.locator('nav[aria-label="会话"]'), scenario).toHaveCount(0)
      continue
    }

    const selected = page.locator('li[aria-current="true"]')
    const count = await selected.count()
    expect(count, `${scenario} selects at most one row`).toBeLessThanOrEqual(1)

    if (count === 0) {
      // The only scenario without a session is the no-workspace state, and it has
      // to say so rather than showing a header that disagrees with the sidebar.
      expect(scenario, scenario).toBe('empty-no-workspace')
      await expect(page.getByText(/未选择工作区：发送已禁用/)).toBeVisible()
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('未选择工作区')
      continue
    }

    const title = await page.getByRole('heading', { level: 1 }).innerText()
    expect(await selected.innerText(), scenario).toContain(title)
  }
})

test('retrieval rows separate a hit, a miss and a failure', async ({ page }) => {
  await page.goto('/?state=memory-hit')
  const hit = page.getByRole('region', { name: '检索与记忆' })
  await expect(hit.getByText('已命中')).toBeVisible()
  await expect(hit.getByText('3 处来源')).toBeVisible()
  // 来源与说明**默认折叠**（T39：检索详情、每轮统计、审批参数默认折叠），展开后才断言内容。
  await hit.getByText('检索与记忆').click()
  // Locators, never content: no retrieved text is shown.
  await expect(hit.getByText('local-agent-web/ARCHITECTURE.md:15.6')).toBeVisible()
  await expect(hit.getByText(/不展示正文、向量或内部评分/)).toBeVisible()
  await expect(hit.getByText(/已写入长期记忆/)).toBeVisible()

  await page.goto('/?state=memory-miss')
  const miss = page.getByRole('region', { name: '检索与记忆' })
  await expect(miss.getByText('未命中')).toBeVisible()
  await miss.getByText('检索与记忆').click()
  await expect(page.getByText(/不会为来源留空位，也不会编造出处/)).toBeVisible()
  await expect(page.getByText(/写入长期记忆失败/)).toBeVisible()
  await expect(page.getByText('检索失败')).toHaveCount(0)

  await page.goto('/?state=memory-failed')
  await expect(page.getByText('检索失败')).toBeVisible()
  await expect(page.getByText(/本地检索索引不可用/)).toBeVisible()
  await expect(page.getByText(/回答中没有引用来源/)).toBeVisible()
  await expect(page.getByText('未命中')).toHaveCount(0)
})

test('the disabled input explains itself instead of only greying out', async ({ page }) => {
  await page.goto('/?state=empty-no-workspace')
  const input = page.getByRole('textbox', { name: '给智能体发消息' })
  await expect(input).toBeDisabled()
  const reason = page.getByText(/未选择工作区：发送已禁用/)
  await expect(reason).toBeVisible()
  const reasonId = await reason.getAttribute('id')
  expect(reasonId).toBeTruthy()
  await expect(input).toHaveAttribute('aria-describedby', reasonId ?? '')
  // No session is open, so the sidebar must not highlight one.
  await expect(page.locator('li[aria-current="true"]')).toHaveCount(0)
})

test('the loading state shows shape only and no invented content', async ({ page }) => {
  await page.goto('/?state=loading-history')
  await expect(page.getByText('正在读取会话事件…')).toBeVisible()
  await expect(page.getByText(/不会重放模型调用/)).toBeVisible()
  await expect(page.getByRole('log').locator('[aria-hidden="true"] > span')).toHaveCount(6)
  // A skeleton next to real content would claim both are the same thing.
  await expect(page.getByText(/开始一个新会话/)).toHaveCount(0)
})

test('the context meter explains its 口径 on hover and pins it as a panel', async ({ page }) => {
  await page.goto('/?state=shell')
  const ring = page.getByRole('button', { name: '展开上下文详情' })
  // 原型页不接 handler，所以圆环连 `aria-expanded` 都不声明——惰性按钮不谎称展开态。
  // 真服务页（`LiveApp`）接上 `onToggleMeter` 之后它才是一个真的开关。
  await expect(ring).not.toHaveAttribute('aria-expanded')
  await expect(page.getByRole('tooltip')).toBeHidden()

  await ring.hover()
  const tooltip = page.getByRole('tooltip')
  await expect(tooltip).toBeVisible()
  await expect(tooltip.getByText(/窗口必须由配置或可信的能力检测确认/)).toBeVisible()
  await expect(tooltip).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('context-meter-hover.png')

  // The pinned panel carries the same 口径, so it does not depend on a pointer.
  await page.goto('/?state=meter-open')
  const panel = page.getByRole('region', { name: '上下文详情' })
  await expect(panel).toBeVisible()
  await expect(panel.getByText(/旧轮次仍显示当时的窗口/)).toBeVisible()
  await expect(page.getByRole('tooltip')).toHaveCount(0)
})

test('the collapsed sidebar keeps a usable rail and hides no selection', async ({ page }) => {
  await page.goto('/?state=sidebar-collapsed')
  await expect(page.locator('aside[data-collapsed="true"]')).toBeVisible()
  await expect(page.locator('nav[aria-label="会话"]')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '新建会话' })).toBeVisible()
  await expect(page.getByRole('button', { name: '展开侧边栏' })).toBeVisible()
  // The session header stays: only the navigation is folded away.
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('给 README 补一段快速开始')
})

test('each server-side conflict code states its own next step', async ({ page }) => {
  await page.goto('/?state=error-codes')
  await expect(page.getByText('SESSION_BUSY')).toHaveCount(1)
  await expect(page.getByText('IDEMPOTENCY_CONFLICT')).toHaveCount(1)
  await expect(page.getByText('RECOVERY_REQUIRED')).toHaveCount(1)

  await expect(page.getByText('当前一轮结束前不能重发，也不会进入队列，且无法中止。')).toBeVisible()
  await expect(page.getByText('内容不一致，必须先确认上一次的结果，不能直接重发。')).toBeVisible()
  await expect(page.getByText('需要人工核对上一次运行的记录；不会自动重提或再次执行工具。')).toBeVisible()

  // None of them offers retry, and none of them falls back to the config sentence.
  await expect(page.getByRole('button', { name: '重新发送' })).toHaveCount(0)
  await expect(page.getByText(/需先处理配置/)).toHaveCount(0)

  // The recovery card must not read as "the app will finish the rest by itself".
  await expect(page.getByText(/应用不会自动补执行未完成的步骤/)).toBeVisible()
  await expect(page.getByText(/不会自动重提同一输入/)).toBeVisible()
})

/**
 * Narrow-desktop proof for the third conflict card.
 *
 * At 1280×800 its tail sits below the fold, so the first screenshot alone cannot
 * substantiate "the whole hint is readable". This capture scrolls the pane to the
 * end and asserts the card and its hint are fully in the viewport.
 */
test('the recovery card is fully readable after scrolling to the end', async ({ page }) => {
  await page.goto('/?state=error-codes')
  const card = page.getByRole('region', { name: '恢复错误' })
  await card.getByText('诊断信息').click()
  await scrollConversationToEnd(page)

  // The whole card, not just its text: the record claims the card is fully in the
  // viewport, so the assertion has to say the same thing.
  await expect(card).toBeInViewport({ ratio: 1 })
  await expect(card.getByText('RECOVERY_REQUIRED')).toBeInViewport({ ratio: 1 })
  await expect(card.getByText('需要人工核对上一次运行的记录；不会自动重提或再次执行工具。')).toBeInViewport({
    ratio: 1,
  })
  await expect(card.getByText(/应用不会自动补执行未完成的步骤/)).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('error-codes-bottom.png')
})

/**
 * Matrix 20 / 21 — the trace tab: the table state and the row inspector.
 *
 * `?state=trace` opens on the trace tab of the same session as `tools`, so the two
 * tabs are two views of one data set rather than two fixtures that could disagree.
 */
test('the trace table lists every tool call and opens an inspector on a row', async ({ page }) => {
  await page.goto('/?state=trace')
  await expect(page.getByRole('tab', { name: '轨迹' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.locator('[data-trace-row]')).toHaveCount(7)
  // The three columns matrix 20 asks for, plus the timeline order.
  await expect(page.getByRole('columnheader', { name: '操作' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '目标' })).toBeVisible()
  await expect(page.getByRole('columnheader', { name: '结果' })).toBeVisible()
  await expect(page.getByText('pnpm run check').first()).toBeVisible()

  await page.getByRole('button', { name: '查看运行命令（失败）的详情' }).click()
  const inspector = page.getByRole('region', { name: '轨迹检查器' })
  await expect(inspector).toBeVisible()
  for (const section of ['操作详情', '时间']) {
    await expect(inspector.getByRole('heading', { name: section })).toBeAttached()
  }
  await expect(inspector.getByText('查看参数')).toBeVisible()
  await settle(page)
  await expect(page).toHaveScreenshot('trace-inspector.png')

  await inspector.getByRole('heading', { name: '时间' }).scrollIntoViewIfNeeded()
  await settle(page)
  await expect(page).toHaveScreenshot('trace-inspector-bottom.png')
})

test('the trace tab is reachable from a conversation and back again', async ({ page }) => {
  await page.goto('/?state=shell')
  await expect(page.getByRole('log', { name: '对话' })).toBeVisible()

  await page.getByRole('tab', { name: '轨迹' }).click()
  await expect(page.getByRole('table')).toBeVisible()
  // The two tabs are alternatives: the conversation is replaced, not stacked under.
  await expect(page.getByRole('log', { name: '对话' })).toHaveCount(0)

  await page.getByRole('tab', { name: '对话' }).click()
  await expect(page.getByRole('log', { name: '对话' })).toBeVisible()
  await expect(page.getByRole('table')).toHaveCount(0)
})

test('a running turn blocks sending without promising a stop control', async ({ page }) => {
  await page.goto('/?state=running-empty')
  await expect(page.getByText('正在分析你的请求…')).toBeVisible()
  await expect(page.getByText('任务进行中：可以先继续写草稿，但要等这一轮结束才能发送。')).toBeVisible()
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled()

  // v0.4 exposes no `cancel_task()`, so no stop control may be rendered at all
  // (ARCHITECTURE §15.2) — a button here would promise a cancellation that the
  // backend cannot perform.
  await expect(page.getByRole('button', { name: /停止/ })).toHaveCount(0)

  // Drafting stays possible while the turn runs.
  await expect(page.getByRole('textbox', { name: '给智能体发消息' })).toBeEnabled()
})

test('settings lists every connection state and keeps deletion confirmed', async ({ page }, testInfo) => {
  // 同上：设置对话框在 640 下比视口高，`ratio: 1` 不可能满足。
  test.skip(testInfo.project.name === '640x800', '窄窗下对话框高于视口')
  await page.goto('/?state=settings-models')
  const dialog = page.getByRole('dialog', { name: '设置' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('article')).toHaveCount(6)
  await expect(dialog.getByText('能力不支持')).toBeVisible()
  // Exact match: "设为默认" buttons also contain the word.
  await expect(dialog.getByText('默认', { exact: true })).toHaveCount(1)

  // The list is taller than the dialog, so the middle states need their own
  // capture: "检测中", the plaintext-transport mark, "检测失败" and
  // "能力不支持" must be fully visible, not just present in the DOM. Aligning the
  // third card to the top of the scroll container (rather than scrolling it
  // minimally into view) is what brings the two cards below it fully in range.
  const testing = dialog.getByRole('article', { name: '连接 本地端点' })
  await testing.evaluate((node) => node.scrollIntoView({ block: 'start' }))
  await expect(testing).toBeInViewport({ ratio: 1 })
  await expect(testing.getByText('明文传输')).toBeInViewport({ ratio: 1 })
  await expect(dialog.getByText('检测失败')).toBeInViewport({ ratio: 1 })
  await expect(dialog.getByText('能力不支持')).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('settings-models-mid.png')

  // The confirmation buttons are the point of the last card, so they have to be
  // reachable rather than merely present further down the list.
  const confirm = dialog.getByRole('button', { name: '确认删除' })
  await confirm.scrollIntoViewIfNeeded()
  await expect(confirm).toBeInViewport({ ratio: 1 })
  await expect(dialog.getByRole('button', { name: '取消' })).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('settings-models-confirm.png')
})

test('settings empty state explains the next step and warns about the credential store', async ({ page }) => {
  await page.goto('/?state=settings-empty')
  const dialog = page.getByRole('dialog', { name: '设置' })
  await expect(dialog.getByText('还没有模型连接')).toBeVisible()
  await expect(dialog.getByRole('article')).toHaveCount(0)
  await expect(dialog.getByText(/密钥只保存在本次进程内，重启后需要重新录入/)).toBeVisible()

  const text = await page.locator('body').innerText()
  expect(text).not.toMatch(/sk-[A-Za-z0-9]{8,}/)
})

test('the model menu states that a switch applies to the next submit only', async ({ page }) => {
  await page.goto('/?state=model-menu')
  await expect(page.getByRole('region', { name: '模型选择' })).toBeVisible()
  await expect(page.getByText('已选择 模型 B · 从下一轮开始生效。')).toBeVisible()
  await expect(page.getByText('正在运行的一轮：模型 A（本轮已固定）')).toBeVisible()
  await expect(page.locator('[aria-pressed="true"]')).toHaveCount(1)
  // The chip is the selector, so it must agree with the menu and not with the
  // still-pinned running turn.
  await expect(page.getByRole('button', { name: '模型 B · 上下文 200k' })).toBeVisible()
})

test('the shipped entry is the real shell and the prototype banner is gone', async ({ page }) => {
  // 真实壳会打 /api：把边界桩住，这条用例只验"入口是谁"，不验后端行为（那是 live 用例的事）。
  await page.route('**/api/**', (route) => route.fulfill({ status: 200, json: [] }))

  // 标识条整体删除（T30）：连 `debug=1` 也不再显示——产品入口已经不走 fixture 了。
  await page.goto('/?debug=1&state=shell')
  await expect(page.getByText('静态 fixture · 未接 Graph 与应用服务')).toHaveCount(0)

  await page.goto('/')
  await expect(page.locator('[data-scenario="live"]')).toBeVisible()
})

/**
 * The tool flow is taller than one screen, so "the card exists" is not evidence
 * that it can be reviewed. These two captures prove the read window and the
 * three tail cards become fully visible once scrolled to.
 */
test('the read window is fully visible after scrolling to it', async ({ page }) => {
  await page.goto('/?state=tools')
  const readCard = page.getByRole('region', { name: '读取结果' })
  await readCard.scrollIntoViewIfNeeded()
  await expect(readCard.getByText('第 41–45 行 / 共 196 行')).toBeInViewport({ ratio: 1 })
  await expect(readCard.getByText('export interface ReadBlockProps {')).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('tools-read.png')
})

test('the tail cards are fully visible after scrolling to the end', async ({ page }, testInfo) => {
  // 同上：窄窗下这几张卡片各自都比视口高。
  test.skip(testInfo.project.name === '640x800', '窄窗下元素高于视口')
  await page.goto('/?state=tools')
  await scrollConversationToEnd(page)

  await expect(page.getByRole('region', { name: '任务清单' }).getByText('1/4 已完成')).toBeInViewport({
    ratio: 1,
  })
  await expect(page.getByText('工具返回了结构化结果')).toBeInViewport({ ratio: 1 })
  // A successful command keeps the success treatment and stays fully readable.
  await expect(page.getByText('退出码 0')).toBeInViewport({ ratio: 1 })
  await settle(page)
  await expect(page).toHaveScreenshot('tools-bottom.png')
})

/**
 * v0.5 T56：能力面板的尾巴也必须滚动后**完整可读**。
 *
 * 第一屏（`capabilities.png`）只证明得了"最后两行在折线以下"——而"缺什么、怎么装"恰恰是下面
 * 那两行要说的话（§16.6 要求不可用必须给可照做提示）。被切成一半的提示等于没写，所以这里按
 * `diff|errors|tools-bottom` 的既有做法：滚到尾巴、断言**完整**进视口（`ratio: 1`），再留一张图。
 *
 * 面板自己负责滚动（`.dialog { max-height: 80vh; overflow: auto }`），不需要滚页面。
 */
test('the capability panel tail is fully readable after scrolling', async ({ page }) => {
  await page.goto('/?state=capabilities')
  const dialog = page.getByRole('dialog', { name: '能力与档位' })

  const last = dialog.getByText('按需加载技能')
  await last.scrollIntoViewIfNeeded()
  await expect(last).toBeInViewport({ ratio: 1 })
  // 缺依赖那条的安装提示：这一屏要能读到"缺什么、怎么装"的完整句子。
  await expect(dialog.getByText(/安装 Git（Windows/)).toBeInViewport({ ratio: 1 })

  await settle(page)
  await expect(page).toHaveScreenshot('capabilities-bottom.png')
})
