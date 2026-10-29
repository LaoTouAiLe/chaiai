/* global process */
import { execSync, spawn } from 'node:child_process'
import { closeSync, mkdirSync, openSync } from 'node:fs'
import { once } from 'node:events'
import { resolve } from 'node:path'

/** Own the Python process directly; Windows shell descendants can retain long-lived SSE pipes. */
export default async function setup() {
  const frontend = resolve(import.meta.dirname, '..')
  execSync('npm run build', { cwd: frontend, windowsHide: true, stdio: 'pipe' })
  mkdirSync(resolve(frontend, 'test-results'), { recursive: true })
  const log = openSync(resolve(frontend, 'test-results/question-queue-server.log'), 'w')
  const python = resolve(frontend, process.platform === 'win32'
    ? '../.venv/Scripts/python.exe' : '../.venv/bin/python')
  const server = spawn(python, [resolve(frontend, '../scripts/serve_question_queue.py'), '--port', '8131'], {
    cwd: resolve(frontend, '..'), windowsHide: true, stdio: ['ignore', log, log],
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' },
  })
  closeSync(log)
  let startupError: Error | null = null
  server.on('error', error => { startupError = error })
  const stop = async () => {
    if (server.exitCode !== null) return
    const exited = once(server, 'exit')
    server.kill('SIGTERM')
    await exited
  }
  try {
    const deadline = Date.now() + 120_000
    while (Date.now() < deadline) {
      if (startupError || server.exitCode !== null) throw startupError ?? new Error('隔离场景服务启动失败，请查看 test-results/question-queue-server.log')
      try {
        const response = await fetch('http://127.0.0.1:8131/api/health')
        if (response.ok) return stop
      } catch { /* The owned server is still starting. */ }
      await new Promise(resolve => setTimeout(resolve, 200))
    }
    throw new Error('隔离场景服务启动超时')
  } catch (error) {
    await stop()
    throw error
  }
}
