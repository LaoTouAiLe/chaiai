import { useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

import { Button } from '../../components/ui/Button'
import { isDarkTheme } from '../tool/highlight'
import styles from './TerminalPane.module.css'

/**
 * 一个终端 = 一个右栏标签（2026-10-28）。
 *
 * 与旧实现的区别，三处都是用户明确要求的：
 *   1. **没有审批卡**：终端在打开标签时就由后端直接启动；
 *   2. **没有「新建」**：多开终端＝多开标签，由 `PanelShell` 负责；这里只连一个 `terminalId`；
 *   3. **没有「结束」**：关闭标签即结束该终端，进程不在后台残留。
 *
 * 配色跟随应用主题（`data-theme`，由 `PreferencesProvider` 写入）：原先写死深色，
 * 浅色主题下终端是一块突兀的黑。状态行从顶部挪到底部工具栏，把整个上方让给终端内容。
 */
const DARK_THEME = { background: '#16181d', foreground: '#e1e4eb' }
const LIGHT_THEME = { background: '#fbfbfc', foreground: '#1f2328' }

export default function TerminalPane({ terminalId }: { terminalId: string }) {
  const [state, setState] = useState('正在连接…')
  // 重连＝换一个 key 重新挂载 emulator，避免在同一个 xterm 实例上叠两次 WS 监听。
  const [revision, setRevision] = useState(0)
  return (
    <section className={styles.pane} aria-label="终端">
      <TerminalScreen key={`${terminalId}:${revision}`} terminalId={terminalId} onState={setState} />
      <div className={styles.toolbar}>
        <span className={styles.note} role="status">{state}</span>
        <Button size="sm" onClick={() => setRevision((value) => value + 1)}>重连</Button>
      </div>
    </section>
  )
}

function TerminalScreen({ terminalId, onState }: {
  terminalId: string
  onState: (text: string) => void
}) {
  const element = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const host = element.current
    if (!host) return
    const terminal = new Terminal({ cursorBlink: true, scrollback: 2000, fontSize: 12,
      fontFamily: 'Consolas, monospace', allowProposedApi: false,
      theme: isDarkTheme() ? DARK_THEME : LIGHT_THEME })
    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(host)
    // 主题切换改的是根节点的 `data-theme`，不触发 React 渲染，xterm 也不会自己重绘。
    const themeObserver = new MutationObserver(() => {
      terminal.options.theme = isDarkTheme() ? DARK_THEME : LIGHT_THEME
    })
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const url = new URL(`/api/terminals/${encodeURIComponent(terminalId)}/stream`, window.location.href)
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    const socket = new WebSocket(url)
    socket.binaryType = 'arraybuffer'
    let disposed = false
    let exited = false
    const resize = () => {
      if (disposed || host.clientWidth === 0 || host.clientHeight === 0) return
      fit.fit()
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize',
        cols: Math.max(20, Math.min(400, terminal.cols)), rows: Math.max(5, Math.min(200, terminal.rows)) }))
    }
    socket.onopen = () => { if (!disposed) { onState('已连接'); resize(); terminal.focus() } }
    socket.onmessage = event => {
      if (disposed) return
      if (event.data instanceof ArrayBuffer) { terminal.write(new Uint8Array(event.data)); return }
      try {
        const message = JSON.parse(event.data as string)
        if (message.type === 'reset') terminal.reset()
        if (message.type === 'truncated') { terminal.reset(); onState('较早输出已截断，显示最近内容') }
        if (message.type === 'exit') {
          exited = true
          const code = (message.terminal as { exit_code?: number | null } | undefined)?.exit_code
          onState(code == null ? '终端已结束' : `终端已结束（退出码 ${code}）`)
        }
      } catch { onState('收到无法解析的终端消息') }
    }
    socket.onclose = event => {
      if (!disposed && !exited) onState(event.reason || '连接已断开，可点「重连」')
    }
    socket.onerror = () => { if (!disposed) onState('连接失败；请检查服务或其他页面的连接') }
    const input = terminal.onData(text => {
      if (socket.readyState !== WebSocket.OPEN) return
      const bytes = new TextEncoder().encode(text)
      if (bytes.length > 16384 || socket.bufferedAmount > 65536) { onState('输入过长或连接拥堵，本次输入未发送'); return }
      socket.send(bytes)
    })
    const observer = new ResizeObserver(resize)
    observer.observe(host)
    resize()
    return () => {
      disposed = true
      themeObserver.disconnect()
      observer.disconnect()
      input.dispose()
      socket.close()
      terminal.dispose()
    }
  }, [terminalId, onState])
  return <div ref={element} className={styles.emulator} aria-label="终端输入输出" />
}
