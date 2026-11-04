/* global process */
import { execSync, spawn } from 'node:child_process'
import { once } from 'node:events'
import { closeSync, mkdtempSync, openSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

/** Separate data and directly owned child processes; keep the user's preview untouched. */
export default async function setup() {
  const frontend = resolve(import.meta.dirname, '..')
  execSync('npm run build', { cwd: frontend, windowsHide: true, stdio: 'pipe' })
  const run = mkdtempSync(resolve(frontend, '../../.playwright-cli/long-task-'))
  const environment: Record<string, string | undefined> = {
    ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8',
  }
  delete environment.SSLKEYLOGFILE
  const python = resolve(frontend, process.platform === 'win32' ? '../.venv/Scripts/python.exe' : '../.venv/bin/python')
  const servers: ReturnType<typeof spawn>[] = []
  const stop = async () => {
    for (const server of servers) {
      if (server.exitCode !== null || server.signalCode !== null) continue
      const exited = once(server, 'exit')
      server.kill('SIGTERM')
      await exited
    }
  }
  try {
    for (const [scenario, port] of [['streaming', 8140], ['background', 8141], ['reading', 8142]] as const) {
      const url = `http://127.0.0.1:${port}/api/health`
      let occupied = false
      try { occupied = (await fetch(url, { signal: AbortSignal.timeout(800) })).ok } catch { /* Not bound. */ }
      if (occupied) throw new Error(`验收端口 ${port} 已占用，没有复用外部服务`)
      const output = openSync(resolve(run, `${scenario}.log`), 'w')
      const server = spawn(python, [resolve(frontend, '../scripts/serve_reading_acceptance.py'),
        '--port', String(port), '--scenario', scenario, '--data', resolve(run, scenario)], {
        cwd: resolve(frontend, '..'), windowsHide: true, stdio: ['ignore', output, output], env: environment,
      })
      closeSync(output)
      servers.push(server)
      let startupError: Error | null = null
      server.on('error', error => { startupError = error })
      const deadline = Date.now() + 90_000
      let ready = false
      while (Date.now() < deadline) {
        if (startupError || server.exitCode !== null) throw startupError ?? new Error(`${scenario} 验收服务退出；日志 ${run}`)
        try { ready = (await fetch(url, { signal: AbortSignal.timeout(1000) })).ok } catch { /* Starting. */ }
        if (ready) break
        await new Promise(resolve => setTimeout(resolve, 200))
      }
      if (!ready) throw new Error(`${scenario} 验收服务启动超时；日志 ${run}`)
    }
    writeFileSync(resolve(run, 'run.json'), JSON.stringify({ run, ports: [8140, 8141, 8142] }, null, 2))
    return stop
  } catch (error) {
    await stop()
    throw error
  }
}
