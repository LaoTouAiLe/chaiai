import { useCallback, useEffect, useRef, useState } from 'react'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'

import { decideTerminal, listTerminals, requestTerminal, stopTerminal, type TerminalDto } from '../../api/client'
import { Button } from '../../components/ui/Button'
import styles from './TerminalPane.module.css'

const LABELS: Record<TerminalDto['status'], string> = {
  pending: '等待批准', running: '运行中', rejected: '已拒绝', expired: '审批已过期',
  exited: '已退出', stopped: '已结束', failed: '启动失败',
}

export default function TerminalPane({ workspaceId }: { workspaceId: string | null }) {
  const [items, setItems] = useState<TerminalDto[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [available, setAvailable] = useState(false)
  const [reason, setReason] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const generation = useRef(0)
  // 自动开终端只做一次。
  const autoRequested = useRef(false)
  const current = items.find(item => item.approval_id === selected)
  const update = useCallback((item: TerminalDto) => {
    generation.current += 1
    setItems(previous => previous.some(old => old.approval_id === item.approval_id)
      ? previous.map(old => old.approval_id === item.approval_id ? item : old) : [...previous, item])
    setSelected(item.approval_id)
  }, [])

  useEffect(() => {
    if (!workspaceId) return
    let disposed = false
    const refresh = () => {
      const requestedGeneration = generation.current
      return listTerminals(workspaceId).then(result => {
        if (disposed || generation.current !== requestedGeneration) return
        setItems(result.items)
        setAvailable(result.available)
        setReason(result.reason ?? null)
        setSelected(previous => previous ?? [...result.items].reverse().find(item => item.status === 'running' || item.status === 'pending')?.approval_id ?? null)
        // **点开就是终端**（2026-10-23）：没有起始页、不用先点「新建」。
        // 一次就够（`autoRequested` 守卫生效），用户主动结束之后不再纠缠着重开。
        if (result.items.length === 0 && result.available && !autoRequested.current) {
          autoRequested.current = true
          void requestTerminal(workspaceId)
            .then(item => { if (!disposed) update(item) })
            .catch((cause: Error) => { if (!disposed) setError(cause.message) })
        }
      }).catch((cause: Error) => { if (!disposed) setError(cause.message) })
    }
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 5000)
    return () => { disposed = true; window.clearInterval(timer) }
  }, [workspaceId, update])

  async function act(operation: () => Promise<TerminalDto>) {
    setBusy(true)
    setError(null)
    try { update(await operation()) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '请求失败，请重试。') }
    finally { setBusy(false) }
  }

  if (!workspaceId) return <p>选择工作区后才能申请终端。</p>
  return <section className={styles.pane} aria-label="受控终端">
    <div className={styles.toolbar}>
      <select aria-label="选择终端" value={selected ?? ''} onChange={event => setSelected(event.target.value)}>
        <option value="" disabled>选择终端</option>
        {items.map((item, index) => <option key={item.approval_id} value={item.approval_id}>
          终端 {index + 1} · {LABELS[item.status]}
        </option>)}
      </select>
      <Button size="sm" disabled={busy || !available} onClick={() => { void act(() => requestTerminal(workspaceId)) }}>新建</Button>
      {current?.status === 'running' && current.terminal_id ? <Button size="sm" variant="danger" disabled={busy}
        onClick={() => { void act(() => stopTerminal(current.terminal_id!)) }}>结束</Button> : null}
    </div>
    {reason ? <p className={styles.note}>{reason}</p> : null}
    {error ? <p role="alert" className={styles.note}>{error}</p> : null}
    {!current ? <p className={styles.note}>在当前工作区启动交互式命令行。每个新终端都需要你批准。</p> : null}
    {current?.status === 'pending' ? <div className={styles.approval}>
      <strong>批准启动终端</strong>
      <dl><dt>工作目录</dt><dd>{current.cwd}</dd><dt>Shell</dt><dd>{current.shell}</dd></dl>
      <p>{current.permission_mode === 'read-only' ? '当前为只读模式，不允许写入文件。' : '可写当前工作区和专用临时目录；已有受保护目录仍只读。'}</p>
      <p>批准后，终端内输入的命令不再逐条审批。可读取本机文件并访问网络；切换目录不会扩大写入范围。</p>
      <p>5 分钟内有效。结束终端会关闭它启动的全部进程。</p>
      <div className={styles.toolbar}>
        <Button variant="primary" disabled={busy} onClick={() => { void act(() => decideTerminal(current.approval_id, 'approve')) }}>批准并启动</Button>
        <Button disabled={busy} onClick={() => { void act(() => decideTerminal(current.approval_id, 'reject')) }}>拒绝</Button>
      </div>
    </div> : null}
    {current && current.status !== 'pending' ? <p className={styles.note}>
      {LABELS[current.status]}{current.exit_code != null ? ` · 退出码 ${current.exit_code}` : ''}
      {current.detail ? ` · ${current.detail}` : ''}
    </p> : null}
    {current?.terminal_id ? <>
      <TerminalScreen key={`${current.terminal_id}:${revision}`} terminalId={current.terminal_id} onExit={update} />
      <div className={styles.toolbar}>
        <span className={styles.note}>关闭面板保留进程；30 分钟无输入后结束。</span>
        <Button size="sm" onClick={() => setRevision(value => value + 1)}>重连</Button>
      </div>
    </> : null}
  </section>
}

function TerminalScreen({ terminalId, onExit }: { terminalId: string; onExit: (view: TerminalDto) => void }) {
  const element = useRef<HTMLDivElement>(null)
  const [state, setState] = useState('正在连接…')
  useEffect(() => {
    const host = element.current
    if (!host) return
    const terminal = new Terminal({ cursorBlink: true, scrollback: 2000, fontSize: 12,
      fontFamily: 'Consolas, monospace', allowProposedApi: false,
      theme: { background: '#16181d', foreground: '#e1e4eb' } })
    const fit = new FitAddon()
    terminal.loadAddon(fit)
    terminal.open(host)
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
    socket.onopen = () => { if (!disposed) { setState('已连接'); resize(); terminal.focus() } }
    socket.onmessage = event => {
      if (disposed) return
      if (event.data instanceof ArrayBuffer) { terminal.write(new Uint8Array(event.data)); return }
      try {
        const message = JSON.parse(event.data as string)
        if (message.type === 'reset') terminal.reset()
        if (message.type === 'truncated') { terminal.reset(); setState('较早输出已截断，显示最近内容') }
        if (message.type === 'exit') { exited = true; setState('终端已结束'); onExit(message.terminal as TerminalDto) }
      } catch { setState('收到无法解析的终端消息') }
    }
    socket.onclose = event => { if (!disposed && !exited) setState(event.reason || '连接已断开；可点击重连') }
    socket.onerror = () => { if (!disposed) setState('连接失败；请检查服务或其他页面的连接') }
    const input = terminal.onData(text => {
      if (socket.readyState !== WebSocket.OPEN) return
      const bytes = new TextEncoder().encode(text)
      if (bytes.length > 16384 || socket.bufferedAmount > 65536) { setState('输入过长或连接拥堵，本次输入未发送'); return }
      socket.send(bytes)
    })
    const observer = new ResizeObserver(resize)
    observer.observe(host)
    resize()
    return () => { disposed = true; observer.disconnect(); input.dispose(); socket.close(); terminal.dispose() }
  }, [terminalId, onExit])
  return <div className={styles.screen}>
    <p className={styles.note} role="status">{state}</p>
    <div ref={element} className={styles.emulator} aria-label="终端输入输出" />
  </div>
}
