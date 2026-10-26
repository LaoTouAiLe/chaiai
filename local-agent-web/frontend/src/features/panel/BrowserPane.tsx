import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, MouseEvent, PointerEvent } from 'react'

import { createBrowser, type BrowserDto, type BrowserInput, type BrowserMessage } from '../../api/client'
import { Button } from '../../components/ui/Button'
import { browserAddress } from './browserAddress'
import styles from './BrowserPane.module.css'

function modifiers(event: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }) {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0)
}

export default function BrowserPane({ requestId, workspaceId, onSession }: {
  requestId: string
  workspaceId: string | null
  onSession: (id: string, title?: string) => void
}) {
  const [view, setView] = useState<BrowserDto | null>(null)
  const [address, setAddress] = useState('')
  const [phase, setPhase] = useState<'starting' | 'connecting' | 'ready' | 'disconnected'>('starting')
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [prompt, setPrompt] = useState('')
  const viewport = useRef<HTMLDivElement>(null)
  const image = useRef<HTMLImageElement>(null)
  const keyboard = useRef<HTMLTextAreaElement>(null)
  const socket = useRef<WebSocket | null>(null)
  const size = useRef({ width: 520, height: 700 })
  const editingAddress = useRef(false)
  const composing = useRef(false)
  const onSessionRef = useRef(onSession)
  const touch = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const moveTime = useRef(0)
  const mousePress = useRef<{ button: 'left' | 'right' | 'middle'; clicks: number } | null>(null)
  useEffect(() => { onSessionRef.current = onSession }, [onSession])

  const send = (message: BrowserInput) => {
    const connection = socket.current
    if (connection?.readyState !== WebSocket.OPEN) return
    if (connection.bufferedAmount > 65536) { setError('连接繁忙，本次输入未发送，请稍后重试。'); return }
    connection.send(JSON.stringify(message))
  }

  useEffect(() => {
    let disposed = false
    let connection: WebSocket | null = null
    let resizeTimer = 0
    setPhase('starting')
    setError(null)
    const resize = () => {
      window.clearTimeout(resizeTimer)
      resizeTimer = window.setTimeout(() => {
        const host = viewport.current
        if (!host || connection?.readyState !== WebSocket.OPEN || !host.clientWidth || !host.clientHeight) return
        connection.send(JSON.stringify({ action: 'resize', width: Math.max(240, Math.min(1920, host.clientWidth)),
          height: Math.max(200, Math.min(1400, host.clientHeight)) } satisfies BrowserInput))
      }, 80)
    }
    const observer = new ResizeObserver(resize)
    if (viewport.current) observer.observe(viewport.current)
    void createBrowser(requestId, workspaceId).then(result => {
      if (disposed) return
      setView(result)
      onSessionRef.current(result.browser_id, result.url === 'about:blank' ? undefined : result.title)
      setPhase('connecting')
      const url = new URL(`/api/browsers/${encodeURIComponent(result.browser_id)}/stream`, location.href)
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
      if (revision > 0) url.searchParams.set('take_over', 'true')
      connection = new WebSocket(url)
      socket.current = connection
      connection.onopen = () => { if (!disposed) { setPhase('ready'); resize() } }
      connection.onmessage = event => {
        if (disposed) return
        try {
          const message = JSON.parse(event.data as string) as BrowserMessage
          if (message.type === 'frame' && typeof message.data === 'string') {
            size.current = { width: message.width, height: message.height }
            if (image.current) image.current.src = `data:image/jpeg;base64,${message.data}`
          } else if (message.type === 'state') {
            setView(message)
            onSessionRef.current(message.browser_id, message.url === 'about:blank' ? undefined : message.title)
            if (!editingAddress.current) setAddress(message.url === 'about:blank' ? '' : message.url)
            if (message.dialog) setPrompt(previous => previous || message.dialog?.defaultPrompt || '')
          }
        } catch { setError('网页画面接收失败，请重新连接。') }
      }
      connection.onclose = event => {
        if (!disposed) { setPhase('disconnected'); setError(event.reason || '浏览器连接已断开，请重新连接。') }
      }
      connection.onerror = () => { if (!disposed) setError('无法连接浏览器，请检查本机服务后重试。') }
    }).catch((cause: unknown) => {
      if (!disposed) { setPhase('disconnected'); setError(cause instanceof Error ? cause.message : '浏览器启动失败，请重试。') }
    })
    return () => {
      disposed = true
      observer.disconnect()
      window.clearTimeout(resizeTimer)
      connection?.close()
      if (socket.current === connection) socket.current = null
    }
  }, [requestId, workspaceId, revision])

  useEffect(() => {
    const host = viewport.current
    if (!host) return
    const wheel = (event: WheelEvent) => {
      if (socket.current?.readyState !== WebSocket.OPEN || view?.url === 'about:blank') return
      event.preventDefault()
      const box = host.getBoundingClientRect()
      const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? 16 : event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? size.current.height : 1
      send({ action: 'wheel', x: (event.clientX - box.left) * size.current.width / box.width,
        y: (event.clientY - box.top) * size.current.height / box.height,
        delta_x: Math.max(-10000, Math.min(10000, event.deltaX * unit)),
        delta_y: Math.max(-10000, Math.min(10000, event.deltaY * unit)), modifiers: modifiers(event) })
    }
    host.addEventListener('wheel', wheel, { passive: false })
    return () => host.removeEventListener('wheel', wheel)
  }, [view?.url])

  const navigate = (raw: string) => {
    try {
      const url = browserAddress(raw)
      setError(null)
      setAddress(url)
      editingAddress.current = false
      setView(previous => previous ? { ...previous, loading: true, error: null } : previous)
      send({ action: 'navigate', url })
      keyboard.current?.focus({ preventScroll: true })
    } catch (cause) { setError(cause instanceof Error ? cause.message : '请输入有效的网址。') }
  }

  const point = (event: PointerEvent<HTMLDivElement> | MouseEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect()
    return { x: Math.max(0, Math.min(size.current.width - 1, (event.clientX - box.left) * size.current.width / box.width)),
      y: Math.max(0, Math.min(size.current.height - 1, (event.clientY - box.top) * size.current.height / box.height)) }
  }
  const pointer = (event: PointerEvent<HTMLDivElement>, kind: 'down' | 'up' | 'move') => {
    if (phase !== 'ready' || !view || view.url === 'about:blank') return
    const position = point(event)
    if (event.pointerType === 'touch') {
      event.preventDefault()
      if (kind === 'down') {
        touch.current = { ...position, moved: false }
        event.currentTarget.setPointerCapture(event.pointerId)
      } else if (kind === 'move' && touch.current) {
        const previous = touch.current
        const delta = { x: previous.x - position.x, y: previous.y - position.y }
        if (previous.moved || Math.abs(delta.x) + Math.abs(delta.y) > 6) {
          send({ action: 'wheel', ...position, delta_x: delta.x, delta_y: delta.y })
          touch.current = { ...position, moved: true }
        }
      } else if (kind === 'up' && touch.current) {
        if (!touch.current.moved) {
          keyboard.current?.focus({ preventScroll: true })
          send({ action: 'pointer', event: 'down', button: 'left', buttons: 1, ...position })
          send({ action: 'pointer', event: 'up', button: 'left', ...position })
        }
        touch.current = null
      }
      return
    }
    if (kind === 'down') {
      // 保留兼容 mousedown：它的 detail 才包含浏览器计算出的双击/三击次数。
      event.currentTarget.setPointerCapture(event.pointerId)
      return
    }
    event.preventDefault()
    if (kind === 'up') {
      const pressed = mousePress.current
      if (pressed) send({ action: 'pointer', ...position, event: 'up', ...pressed, buttons: 0, modifiers: modifiers(event) })
      mousePress.current = null
      return
    }
    if (kind === 'move') {
      if (performance.now() - moveTime.current < 40) return
      moveTime.current = performance.now()
    }
    send({ action: 'pointer', ...position, event: kind, modifiers: modifiers(event), buttons: event.buttons,
      button: event.buttons === 0 ? 'none' : mousePress.current?.button ?? 'left' })
  }
  const mouseDown = (event: MouseEvent<HTMLDivElement>) => {
    if (phase !== 'ready' || !view || view.url === 'about:blank') return
    event.preventDefault()
    keyboard.current?.focus({ preventScroll: true })
    const pressed = { button: event.button === 2 ? 'right' as const : event.button === 1 ? 'middle' as const : 'left' as const,
      clicks: Math.max(1, Math.min(3, event.detail)) }
    mousePress.current = pressed
    send({ action: 'pointer', ...point(event), event: 'down', ...pressed, buttons: event.buttons, modifiers: modifiers(event) })
  }

  const text = (value: string) => {
    if (value.length > 8192) { setError('一次最多输入 8192 个字符，请分次粘贴。'); return }
    if (value) send({ action: 'text', text: value })
  }
  const key = (event: KeyboardEvent<HTMLTextAreaElement>, kind: 'down' | 'up') => {
    if (event.nativeEvent.isComposing || composing.current || event.key === 'Process') return
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v') return
    if (event.key.length > 1 || event.ctrlKey || event.metaKey || event.altKey) event.preventDefault()
    send({ action: 'key', event: kind, key: event.key, code: event.code, key_code: event.keyCode, modifiers: modifiers(event) })
  }
  const ready = phase === 'ready'
  const blank = !view || view.url === 'about:blank'

  return <section className={styles.pane} aria-label="内置浏览器" aria-busy={phase === 'starting' || phase === 'connecting'}>
    <div className={styles.toolbar}>
      <Button variant="icon" size="sm" aria-label="浏览器后退" title="后退" disabled={!ready || !view?.can_back} onClick={() => send({ action: 'back' })}><Icon path="m14 6-6 6 6 6" /></Button>
      <Button variant="icon" size="sm" aria-label="浏览器前进" title="前进" disabled={!ready || !view?.can_forward} onClick={() => send({ action: 'forward' })}><Icon path="m10 6 6 6-6 6" /></Button>
      <Button variant="icon" size="sm" aria-label={view?.loading ? '停止加载网页' : '刷新网页'} title={view?.loading ? '停止加载' : '刷新'} disabled={!ready} onClick={() => send({ action: view?.loading ? 'stop' : 'reload' })}>
        <Icon path={view?.loading ? 'M7 7h10v10H7z' : 'M20 7v5h-5M19 12a7 7 0 1 0-2 5M20 12l-4-6'} />
      </Button>
      <form className={styles.addressForm} onSubmit={event => { event.preventDefault(); navigate(address) }}>
        <input aria-label="浏览器地址" placeholder="输入网址或 localhost:5173" value={address} spellCheck={false} autoComplete="off"
          onFocus={event => { editingAddress.current = true; event.currentTarget.select() }} onBlur={() => { editingAddress.current = false }}
          onChange={event => setAddress(event.target.value)} />
        <button type="submit" aria-label="访问网址" title="访问" disabled={!ready || !address.trim()}><Icon path="M5 12h14m-6-6 6 6-6 6" /></button>
      </form>
    </div>
    <div className={styles.status} role="status">
      <span className={styles.statusDot} data-ready={ready} />
      <span className={styles.title} title={view?.title}>{phase === 'starting' ? '正在启动浏览器…' : phase === 'connecting' ? '正在连接…' : phase === 'disconnected' ? '连接已断开' : blank ? '新标签页' : view?.title}</span>
      {phase === 'disconnected' ? <Button size="sm" variant="ghost" onClick={() => setRevision(value => value + 1)}>重新连接</Button> : null}
    </div>
    {view?.loading ? <div className={styles.progress} aria-label="网页正在加载" /> : null}
    {error || view?.error ? <p className={styles.error} role="alert">{error ?? view?.error}</p> : null}
    {view?.dialog ? <div className={styles.dialog} role="dialog" aria-label="网页提示">
      <p>{view.dialog.message}</p>
      {view.dialog.type === 'prompt' ? <input aria-label="网页提示输入" value={prompt} onChange={event => setPrompt(event.target.value)} /> : null}
      <div><Button size="sm" onClick={() => { send({ action: 'dialog', accept: false }); setPrompt('') }}>取消</Button>
        <Button size="sm" variant="primary" onClick={() => { send({ action: 'dialog', accept: true, text: prompt }); setPrompt('') }}>确定</Button></div>
    </div> : null}
    <div ref={viewport} className={styles.viewport} aria-label="网页交互区域"
      onMouseDown={mouseDown}
      onPointerDown={event => pointer(event, 'down')} onPointerMove={event => pointer(event, 'move')}
      onPointerUp={event => pointer(event, 'up')} onPointerCancel={event => {
        if (event.pointerType !== 'touch') pointer(event, 'up')
        touch.current = null
      }}
      onContextMenu={event => event.preventDefault()}>
      <img ref={image} className={styles.frame} alt="浏览器网页画面" draggable={false} hidden={blank} />
      <textarea ref={keyboard} className={styles.keyboard} aria-label="网页键盘输入" autoCapitalize="off" autoCorrect="off" spellCheck={false}
        onKeyDown={event => key(event, 'down')} onKeyUp={event => key(event, 'up')}
        onCompositionStart={() => { composing.current = true }}
        onCompositionEnd={event => { composing.current = false; text(event.currentTarget.value); event.currentTarget.value = '' }}
        onChange={event => { if (!composing.current) { text(event.currentTarget.value); event.currentTarget.value = '' } }}
        onPaste={event => { event.preventDefault(); text(event.clipboardData.getData('text/plain')) }} />
      {blank ? <div className={styles.welcome}>
        <span className={styles.welcomeIcon}><Icon path="M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18" /></span>
        <h2>网页，就在工作旁边</h2><p>浏览外网，或预览正在开发的本地项目。</p>
        <div className={styles.shortcuts}>
          <button type="button" disabled={!ready} onClick={() => navigate('https://example.org')}>打开示例网站 <span>↗</span></button>
          <button type="button" disabled={!ready} onClick={() => navigate('http://localhost:5173')}>本地项目 <span>localhost:5173</span></button>
        </div>
        <p className={styles.hint}>本地预览需先启动项目服务。切换标签可保留页面；收起后空闲 30 分钟会结束浏览器。</p>
      </div> : null}
      {!ready && !blank ? <div className={styles.disconnected}>连接恢复后即可继续操作</div> : null}
    </div>
  </section>
}

function Icon({ path }: { path: string }) {
  return <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={path} /></svg>
}
