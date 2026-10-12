import { expect, test } from '@playwright/test'

/**
 * 档位切换（§16.6）：真服务 + 真前端。
 *
 * 为什么必须打真服务：这条要证明的是"页面里切一下，**下一次运行真的按新档位绑定能力**，
 * 而且不用重启"。用 `page.route` 打桩能证明"页面会按我编的响应渲染"，证明不了档位真的写进了
 * 设置库、也证明不了刷新之后仍是用户的选择（那正是"存设置库、config 只是出厂默认"这条口径）。
 *
 * 服务由 `playwright.live.config.ts` 拉起（`serve_fake.py`，同源托管产物）。
 */
test('切换档位：能力跟着变、不用重启，刷新后仍是用户的选择', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '能力与档位' }).click()
  const panel = page.getByRole('dialog', { name: '能力与档位' })
  await expect(panel).toBeVisible()

  // 出厂默认是全能力档：写文件能力可用，选择器停在 coder。
  const writeRow = panel.getByRole('listitem').filter({ hasText: '修改工作区文件' })
  await expect(writeRow).toContainText('可用')
  const selector = panel.getByLabel('切换当前档位')
  await expect(selector).toHaveValue('coder')

  // 切到只读档：**同一份面板**立刻改口（响应就是服务端重新算过的完整视图）。
  await selector.selectOption('reviewer')
  await expect(writeRow).toContainText('当前档位没有')
  await expect(selector).toHaveValue('reviewer')

  // 没有重启、也没有重新打开面板——改的是设置库，下一次运行就会按新档位绑定工具。
  await page.reload()
  await page.getByRole('button', { name: '能力与档位' }).click()
  const reopened = page.getByRole('dialog', { name: '能力与档位' })
  await expect(reopened.getByLabel('切换当前档位')).toHaveValue('reviewer')
  await expect(reopened.getByRole('listitem').filter({ hasText: '修改工作区文件' })).toContainText(
    '当前档位没有',
  )
})
