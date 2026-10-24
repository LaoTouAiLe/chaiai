/** 先让隔离服务关闭数据库和 MCP 进程，再交还给 Playwright 回收宿主。 */
export default async function teardown() {
  try {
    const response = await fetch('http://127.0.0.1:8139/__test__/shutdown', {
      method: 'POST', signal: AbortSignal.timeout(5000),
    })
    if (!response.ok) return
    for (let attempt = 0; attempt < 100; attempt += 1) {
      await new Promise(resolve => setTimeout(resolve, 100))
      try {
        await fetch('http://127.0.0.1:8139/api/health', { signal: AbortSignal.timeout(1000) })
      } catch { return }
    }
  } catch { /* 服务启动失败时无需清理。 */ }
}
