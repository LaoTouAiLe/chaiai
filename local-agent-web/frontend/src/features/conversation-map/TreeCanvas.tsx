import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { MapData, MapNode } from './adapters'
import { CARD_HEIGHT, CARD_WIDTH, fitTree, layoutTree, zoomAt, type MapViewport } from './layout'
import styles from './TreeCanvas.module.css'

interface Props {
  data: MapData
  collapsed: ReadonlySet<string>
  searching: boolean
  path: ReadonlySet<string>
  selectedId: string | null
  focusRequest: { id: string; version: number } | null
  onLocateCurrent: (id: string) => void
  onSelect: (node: MapNode) => void
  onToggle: (id: string) => void
}

export function TreeCanvas({ data, collapsed, searching, path, selectedId, focusRequest, onLocateCurrent, onSelect, onToggle }: Props) {
  const layout = useMemo(() => layoutTree(data, collapsed, searching), [data, collapsed, searching])
  const viewport = useRef<HTMLDivElement>(null)
  const initialized = useRef(false)
  const handledFocus = useRef<Props['focusRequest']>(null)
  const drag = useRef<{ id: number; x: number; y: number; origin: MapViewport } | null>(null)
  const [view, setView] = useState<MapViewport>({ x: 40, y: 40, scale: 0.9 })
  const [dragging, setDragging] = useState(false)
  // 全图布局为底部控制条留出空间，末端卡片不会被缩放按钮遮挡。
  const dimensions = () => {
    const width = viewport.current?.clientWidth || 800
    return { width, height: Math.max(120, (viewport.current?.clientHeight || 500) - (width < 400 ? 120 : 80)) }
  }
  const focusCard = useCallback((id: string | null) => {
    const card = layout.cards.find((item) => item.node.id === id)
    if (card === undefined) return
    const { width, height } = dimensions()
    setView((current) => ({ ...current, x: width / 2 - (card.x + CARD_WIDTH / 2) * current.scale,
      y: height / 2 - (card.y + CARD_HEIGHT / 2) * current.scale }))
  }, [layout])
  const fit = useCallback(() => {
    const { width, height } = dimensions()
    setView(fitTree(layout, width, height))
  }, [layout])

  useLayoutEffect(() => {
    if (initialized.current || layout.cards.length === 0) return
    initialized.current = true
    const { width, height } = dimensions()
    const fitted = fitTree(layout, width, height)
    // 长链默认保留可读字号，从根节点开始；“适应全图”提供全局总览。
    const root = layout.cards[0]!
    setView(searching || fitted.scale >= 0.75 ? fitted : { scale: 0.9, x: 32 - root.x * 0.9,
      y: height / 2 - (root.y + CARD_HEIGHT / 2) * 0.9 })
  }, [layout, searching])
  useEffect(() => {
    if (focusRequest === null || handledFocus.current === focusRequest || !layout.cards.some((card) => card.node.id === focusRequest.id)) return
    focusCard(focusRequest.id)
    handledFocus.current = focusRequest
  }, [focusRequest, focusCard, layout])
  useEffect(() => {
    const element = viewport.current
    if (element === null || typeof ResizeObserver === 'undefined') return
    let width = element.clientWidth, height = element.clientHeight
    const observer = new ResizeObserver(() => {
      if (width === element.clientWidth && height === element.clientHeight) return
      width = element.clientWidth; height = element.clientHeight
      if (focusRequest !== null) focusCard(focusRequest.id)
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [focusRequest, focusCard])
  useEffect(() => {
    const element = viewport.current
    if (element === null) return
    // 使用非 passive 监听，缩放画布时不同时滚动整个页面。
    const wheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = element.getBoundingClientRect()
      setView((current) => zoomAt(current, current.scale * Math.exp(-Math.max(-200, Math.min(200, event.deltaY)) * 0.003),
        event.clientX - rect.left, event.clientY - rect.top))
    }
    element.addEventListener('wheel', wheel, { passive: false })
    return () => element.removeEventListener('wheel', wheel)
  }, [])
  function zoom(factor: number) {
    const { width, height } = dimensions()
    setView((current) => zoomAt(current, current.scale * factor, width / 2, height / 2))
  }
  const tipId = data.branches.find((branch) => branch.active)?.tipId ?? null

  return <div className={styles.canvas}>
    <div ref={viewport} className={`${styles.viewport} ${dragging ? styles.dragging : ''}`}
      role="region" aria-label="树状地图画布" tabIndex={0}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return
        const shifts: Record<string, [number, number]> = { ArrowLeft: [80, 0], ArrowRight: [-80, 0], ArrowUp: [0, 80], ArrowDown: [0, -80] }
        const shift = shifts[event.key]
        if (shift) { event.preventDefault(); setView((current) => ({ ...current, x: current.x + shift[0], y: current.y + shift[1] })) }
      }}
      onPointerDown={(event) => {
        if (event.button !== 0 || (event.target as HTMLElement).closest('button')) return
        event.currentTarget.focus({ preventScroll: true })
        event.currentTarget.setPointerCapture(event.pointerId)
        drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, origin: view }
        setDragging(true)
      }}
      onPointerMove={(event) => {
        const start = drag.current
        if (start === null || start.id !== event.pointerId) return
        setView({ ...start.origin, x: start.origin.x + event.clientX - start.x, y: start.origin.y + event.clientY - start.y })
      }}
      onPointerUp={() => { drag.current = null; setDragging(false) }}
      onPointerCancel={() => { drag.current = null; setDragging(false) }}
      onLostPointerCapture={() => { drag.current = null; setDragging(false) }}>
      <div className={styles.world} data-map-transform="true" style={{ width: layout.width, height: layout.height,
        transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})` }}>
        <svg className={styles.connections} width={layout.width} height={layout.height} aria-hidden="true">
          {layout.edges.map((edge) => <path key={edge.childId} d={edge.path}
            data-parent-id={edge.parentId} data-child-id={edge.childId}
            className={path.has(edge.parentId) && path.has(edge.childId) ? styles.activeEdge : styles.edge} />)}
        </svg>
        {layout.cards.map(({ node, x, y, childCount }) => <article key={node.id} data-map-node={node.id}
          className={`${styles.node} ${path.has(node.id) ? styles.inPath : ''} ${selectedId === node.id ? styles.selected : ''}`}
          style={{ left: x, top: y, width: CARD_WIDTH, height: CARD_HEIGHT }}>
          <button type="button" className={styles.card} data-turn-id={node.id} aria-pressed={selectedId === node.id}
            aria-label={`第${String(node.ordinal)}轮 · ${node.status} ${node.question}`} onClick={() => onSelect(node)}
            onFocus={(event) => { if (event.currentTarget.matches(':focus-visible')) focusCard(node.id) }}>
            <span className={styles.cardTop}><span>第 {node.ordinal} 轮{data.matchedIds.has(node.id) ? ' · 匹配' : ''}</span><span data-state={node.status} className={styles.status}>{node.status}</span></span>
            <strong className={styles.question}>{node.question}</strong>
            <span className={styles.preview}>{node.answerPreview || '暂无回答记录'}</span>
            <span className={styles.cardBottom}><span>{data.branches.find((branch) => branch.id === node.originBranchId)?.title ?? '历史路径'}</span><span>查看详情 ↗</span></span>
          </button>
          <span className={styles.inputPort} aria-hidden="true" /><span className={styles.outputPort} aria-hidden="true" />
          {childCount > 0 ? <button type="button" className={styles.fold} disabled={searching}
            aria-label={`${collapsed.has(node.id) ? '展开' : '折叠'}第${String(node.ordinal)}轮后续`}
            onClick={() => onToggle(node.id)}>{collapsed.has(node.id) ? `+${String(childCount)}` : '−'}</button> : null}
        </article>)}
      </div>
    </div>
    <div className={styles.legend}><span className={styles.dot} />当前路径<span className={styles.otherDot} />其他分支</div>
    <div className={styles.controls} aria-label="地图视图控制">
      <button type="button" aria-label="缩小地图" onClick={() => zoom(1 / 1.2)}>−</button>
      <button type="button" aria-label="恢复百分之百缩放" onClick={() => {
        const { width, height } = dimensions(); setView((current) => zoomAt(current, 1, width / 2, height / 2))
      }}>{Math.round(view.scale * 100)}%</button>
      <button type="button" aria-label="放大地图" onClick={() => zoom(1.2)}>＋</button>
      <span className={styles.divider} />
      <button type="button" onClick={fit}>适应全图</button>
      <button type="button" disabled={tipId === null} onClick={() => { if (tipId !== null) onLocateCurrent(tipId) }}>定位当前分支</button>
    </div>
    <p className={styles.hint}>拖动画布移动 · 滚轮缩放 · 点击卡片查看详情</p>
    {layout.cards.length === 0 ? <p className={styles.empty}>{searching ? '没有匹配的问答，试试其他关键词。' : '发送第一条问题后，这里会出现问答卡片。'}</p> : null}
  </div>
}
