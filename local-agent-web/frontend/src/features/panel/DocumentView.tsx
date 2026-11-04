import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { getDocument, GlobalWorkerOptions, TextLayer, type PDFDocumentProxy } from 'pdfjs-dist'
import worker from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import styles from './PreviewPane.module.css'

GlobalWorkerOptions.workerSrc = worker

/** Keep page placeholders in the scroll flow; only nearby pages own raster buffers. */
function DocumentPage({ document, number, width, zoom, ratio, root }: {
  document: PDFDocumentProxy; number: number; width: number; zoom: number; ratio: number
  root: RefObject<HTMLDivElement | null>
}) {
  const slot = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const text = useRef<HTMLDivElement>(null)
  const [near, setNear] = useState(false)
  const [pageRatio, setPageRatio] = useState<number>()
  const [error, setError] = useState(false)
  const [ready, setReady] = useState(false)
  const [retry, setRetry] = useState(0)
  const pageWidth = Math.max(100, width - 40) * zoom
  useEffect(() => {
    const element = slot.current
    if (!element || !root.current) return
    const observer = new IntersectionObserver(entries => setNear(entries[0]?.isIntersecting ?? false), {
      root: root.current, rootMargin: '700px 0px',
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [root])
  useEffect(() => {
    if (!near) return
    let alive = true
    let cancel: (() => void) | undefined
    let layer: TextLayer | undefined
    setReady(false)
    setError(false)
    void document.getPage(number).then(page => {
      if (!alive || !canvas.current) return
      const original = page.getViewport({ scale: 1 })
      setPageRatio(original.height / original.width)
      const scale = pageWidth / original.width
      const view = page.getViewport({ scale })
      const density = Math.min(window.devicePixelRatio || 1, 2)
      const element = canvas.current
      element.width = Math.ceil(view.width * density)
      element.height = Math.ceil(view.height * density)
      element.style.width = `${view.width}px`
      element.style.height = `${view.height}px`
      const task = page.render({ canvas: element, viewport: view, transform: [density, 0, 0, density, 0, 0] })
      cancel = () => task.cancel()
      if (text.current) {
        text.current.replaceChildren()
        text.current.style.setProperty('--total-scale-factor', String(scale))
        layer = new TextLayer({ textContentSource: page.streamTextContent(), container: text.current, viewport: view })
        void layer.render().catch(() => { /* Raster content remains readable. */ })
      }
      void task.promise.then(() => { if (alive) setReady(true) }).catch(cause => {
        if (alive && cause?.name !== 'RenderingCancelledException') setError(true)
      })
    }).catch(() => { if (alive) setError(true) })
    return () => { alive = false; cancel?.(); layer?.cancel() }
  }, [document, number, near, pageWidth, retry])
  return <div ref={slot} className={styles.pdfPage} data-pdf-page={number}
    style={{ width: pageWidth, height: pageWidth * (pageRatio ?? ratio) }}>
    {near ? <>
      <canvas ref={canvas} aria-label={`文档第 ${number} 页`} data-rendered={ready} />
      <div ref={text} className={styles.pdfText} />
    </> : null}
    {near && !ready && !error ? <span className={styles.pagePlaceholder}>第 {number} 页 · 正在加载</span> : null}
    {error ? <div role="alert" className={styles.pagePlaceholder}>第 {number} 页加载失败<button onClick={() => setRetry(value => value + 1)}>重试本页</button></div> : null}
  </div>
}

export default function DocumentView({ url }: { url: string }) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)
  const [zoom, setZoom] = useState(1)
  const [ratio, setRatio] = useState(1.414)
  const [retry, setRetry] = useState(0)
  const viewport = useRef<HTMLDivElement>(null)
  const frame = useRef(0)
  const anchor = useRef({ page: 1, progress: 0 })
  const [width, setWidth] = useState(500)
  useEffect(() => {
    const element = viewport.current
    if (!element) return
    const observer = new ResizeObserver(() => setWidth(element.clientWidth))
    observer.observe(element)
    return () => { observer.disconnect(); cancelAnimationFrame(frame.current) }
  }, [])
  useEffect(() => {
    let alive = true
    setDocument(null)
    setError('')
    setPage(1)
    anchor.current = { page: 1, progress: 0 }
    if (viewport.current) viewport.current.scrollTop = 0
    const request = getDocument({ url })
    void request.promise.then(async value => {
      const first = await value.getPage(1)
      if (!alive) return
      const view = first.getViewport({ scale: 1 })
      setRatio(view.height / view.width)
      setDocument(value)
    }).catch(() => { if (alive) setError('文档无法预览，请重试或通过“打开方式”查看。') })
    return () => { alive = false; void request.destroy() }
  }, [url, retry])
  // A zoom/width change preserves the reader's page and relative vertical position.
  useLayoutEffect(() => {
    const root = viewport.current
    const target = root?.querySelector<HTMLElement>(`[data-pdf-page="${anchor.current.page}"]`)
    if (root && target) root.scrollTop = target.offsetTop - 20 + target.offsetHeight * anchor.current.progress
  }, [width, zoom])
  const measure = () => {
    const root = viewport.current
    if (!root) return
    const pages = Array.from(root.querySelectorAll<HTMLElement>('[data-pdf-page]'))
    const first = pages.find(element => element.offsetTop + element.offsetHeight > root.scrollTop + 24) ?? pages.at(-1)
    const active = pages.find(element => element.offsetTop + element.offsetHeight > root.scrollTop + root.clientHeight / 2) ?? pages.at(-1)
    if (!active || !first) return
    const number = Number(active.dataset.pdfPage)
    anchor.current = { page: Number(first.dataset.pdfPage), progress: Math.max(0, (root.scrollTop - first.offsetTop + 20) / first.offsetHeight) }
    setPage(number)
  }
  const jump = (number: number) => {
    const root = viewport.current
    const target = root?.querySelector<HTMLElement>(`[data-pdf-page="${number}"]`)
    if (!root || !target) return
    root.scrollTo({ top: target.offsetTop - 20, behavior: 'auto' })
    setPage(number)
  }
  return <div className={styles.document}>
    <div className={styles.readerTools} role="toolbar" aria-label="文档阅读工具">
      <button disabled={page <= 1 || !document} onClick={() => jump(page - 1)} aria-label="上一页">‹</button>
      <span aria-live="polite">{page} / {document?.numPages ?? '—'}</span>
      <button disabled={!document || page >= document.numPages} onClick={() => jump(page + 1)} aria-label="下一页">›</button>
      <span className={styles.spacer} />
      <button onClick={() => setZoom(value => Math.max(.5, value - .25))} aria-label="缩小文档">−</button>
      <button onClick={() => setZoom(1)} title="适应宽度">{Math.round(zoom * 100)}%</button>
      <button onClick={() => setZoom(value => Math.min(3, value + .25))} aria-label="放大文档">+</button>
    </div>
    <div ref={viewport} className={styles.paperViewport} role="region" aria-label="连续文档页面"
      onScroll={() => { cancelAnimationFrame(frame.current); frame.current = requestAnimationFrame(measure) }}>
      {!document && !error ? <p role="status" className={styles.hint}>正在准备文档版式…</p> : null}
      {error ? <div role="alert" className={styles.state}><p>{error}</p><button onClick={() => setRetry(value => value + 1)}>重试预览</button></div> : null}
      {document ? Array.from({ length: document.numPages }, (_, index) => <DocumentPage key={index + 1}
        document={document} number={index + 1} width={width} zoom={zoom} ratio={ratio} root={viewport} />) : null}
    </div>
  </div>
}
