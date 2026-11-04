/* global process */
import { execSync, spawn } from 'node:child_process'
import { once } from 'node:events'
import { closeSync, mkdirSync, openSync } from 'node:fs'
import { resolve } from 'node:path'

/** Own Vite directly so Windows shell pipes cannot keep the acceptance run alive. */
export default async function setup() {
  const frontend = resolve(import.meta.dirname, '..')
  execSync('npm run build', { cwd: frontend, windowsHide: true, stdio: 'pipe' })
  mkdirSync(resolve(frontend, 'test-results'), { recursive: true })
  const log = openSync(resolve(frontend, 'test-results/file-preview.log'), 'w')
  const server = spawn(process.execPath, [resolve(frontend, 'node_modules/vite/bin/vite.js'),
    'preview', '--host', '127.0.0.1', '--port', '4182', '--strictPort'], {
    cwd: frontend, windowsHide: true, stdio: ['ignore', log, log],
  })
  closeSync(log)
  let startupError: Error | null = null
  server.on('error', error => { startupError = error })
  const stop = async () => {
    if (server.exitCode !== null || server.signalCode !== null) return
    const exited = once(server, 'exit')
    server.kill('SIGTERM')
    await exited
  }
  try {
    const deadline = Date.now() + 30_000
    while (Date.now() < deadline) {
      if (startupError || server.exitCode !== null) throw startupError ?? new Error('DSH 界面预览服务启动失败，请查看 test-results/file-preview.log')
      try {
        const response = await fetch('http://127.0.0.1:4182')
        if (response.ok) return stop
      } catch { /* Wait for the owned preview process to bind its port. */ }
      await new Promise(resolve => setTimeout(resolve, 200))
    }
    throw new Error('DSH 界面预览服务启动超时')
  } catch (error) {
    await stop()
    throw error
  }
}
