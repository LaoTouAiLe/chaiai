import { expect, test } from '@playwright/test'
import type { components } from '../src/api/schema.gen'

type Snapshot = components['schemas']['SessionSnapshot']
type Session = components['schemas']['SessionSummaryView']

for (const title of ['百度热榜验收', '哔哩哔哩热榜验收', 'MCP联网验收']) {
  test(`${title}的真实来源在刷新后仍与成功工具一致`, async ({ page }) => {
    const sessions = await (await page.request.get('/api/sessions')).json() as Session[]
    const session = sessions.find(item => item.title === title)
    expect(session, '请先运行隔离真实问题验收脚本').toBeDefined()
    if (session === undefined) return
    const snapshot = await (await page.request.get(`/api/sessions/${session.session_id}`)).json() as Snapshot
    const sources = (snapshot.messages ?? []).flatMap(message => message.kind === 'tool' && message.tool.status === 'ok'
      ? message.tool.web_sources ?? [] : [])
    const urls = new Set(sources.map(source => source.url))
    const cited = new Set(sources.filter(source => source.cited).map(source => source.url))
    expect(urls.size).toBeGreaterThan(0)

    await page.goto('/')
    await page.getByRole('navigation', { name: '会话' }).getByRole('button').filter({ hasText: title }).click()
    const panel = page.getByRole('region', { name: '联网来源' })
    await expect(panel).toBeVisible()
    await expect(panel.getByRole('link')).toHaveCount(urls.size)
    expect(new Set(await panel.getByRole('link').evaluateAll(links => links.map(link => link.getAttribute('href'))))).toEqual(urls)
    await expect(panel.getByText(/回答引用 ·/)).toHaveCount(cited.size)
    await expect(page.getByRole('log', { name: '对话' })).not.toContainText('[web-')
    await page.reload()
    await expect(panel.getByRole('link')).toHaveCount(urls.size)
    await expect(panel.getByText(/回答引用 ·/)).toHaveCount(cited.size)
    if (snapshot.final_result?.state === 'blocked') {
      const explanation = snapshot.final_result.summary.split('\n').find(line => line.trim())?.replace(/^#+\s*/, '')
      expect(explanation).toBeTruthy()
      if (explanation) await expect(page.getByRole('log', { name: '对话' })).toContainText(explanation)
    }
    if (title === 'MCP联网验收') {
      await panel.scrollIntoViewIfNeeded()
      // 落在 `test-results/`（已忽略）：原目标 `docs/ui-reference/` 已于 2026-10-26 移除。
      await page.screenshot({ path: 'test-results/screenshots/web-sources-live-20261015.png' })
    }
  })
}
