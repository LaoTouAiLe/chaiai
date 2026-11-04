import { lazy, Suspense, useEffect, useMemo, useState } from 'react'

import { ApiError, previewArtifact } from '../../api/client'
import { useArtifactsDetail } from '../../api/queries'
import { Close, Copy } from '../../components/icons'
import { Button } from '../../components/ui/Button'
import { MarkdownMessage } from '../conversation/MarkdownMessage'
import { SpreadsheetView } from './SpreadsheetView'
import { OpenInAppMenu } from './OpenInAppMenu'
import { languageOf, useHighlight } from './useHighlight'
import styles from './PreviewPane.module.css'

/** 高亮上限：超过它退回纯文本，但**内容照旧完整**（2026-11-03）。 */
const DocumentView = lazy(() => import('./DocumentView'))
const HIGHLIGHT_LIMIT = 60_000

/**
 * 文件预览（2026-11-03 重做）。
 *
 * 三件事在这一版被推翻：
 *
 * 1. **"看不完"**：原来后端一次只给 20,000 字符、文档只抽 5 页，前端只有一句"只显示开头部分"。
 *    现在按 `next_offset` / `next_page` 续取，能一直看到末尾。
 * 2. **"卡里套卡"**：内容不再是一块带圆角和背景的卡片，而是贴边铺满、自己滚动。
 * 3. **"没有工具条"**：路径、行号、换行、复制、打开方式、关闭都在顶部一条里。
 *
 * 布局仍是**左内容右目录**（用户 2026-11-03 明确要保留的那一点）。
 */
export function PreviewPane({ path, workspaceId, scope, onClose }: {
  path: string
  workspaceId: string | null
  scope: 'workspace' | 'attachments'
  onClose: () => void
}) {
  const first = useArtifactsDetail(path, workspaceId ?? undefined, scope, 'formatted')
  const [extra, setExtra] = useState<string[]>([])
  const [cursor, setCursor] = useState<{ offset: number; page: number } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState<string | null>(null)
  const [wrap, setWrap] = useState(true)
  const [copied, setCopied] = useState(false)
  const [revision, setRevision] = useState(0)
  const [source, setSource] = useState(false)
  const [zoom, setZoom] = useState(1)
  const extension = path.split('.').pop()?.toLowerCase() ?? ''
  const rich = ['md', 'mdx', 'markdown', 'html', 'htm'].includes(extension)

  // 换文件就把分页状态清空：上一份文件的字符偏移对新文件毫无意义。
  useEffect(() => {
    setExtra([])
    setCursor(null)
    setMoreError(null)
    setCopied(false)
    setSource(false)
    setZoom(1)
  }, [path, workspaceId, scope])

  const data = first.data
  const isText = data?.kind === 'text'
  const showCode = isText && (!rich || source)
  const body = isText ? [data.text ?? '', ...extra].join('') : ''
  const hasMore = extra.length === 0 ? data?.has_more === true : cursor !== null
  const html = useHighlight(isText ? body : '', languageOf(path), HIGHLIGHT_LIMIT)

  const loadMore = () => {
    const offset = extra.length === 0 ? (data?.next_offset ?? null) : cursor?.offset ?? null
    const page = extra.length === 0 ? (data?.next_page ?? null) : cursor?.page ?? null
    if (offset === null || page === null) return
    setLoadingMore(true)
    setMoreError(null)
    void previewArtifact(path, workspaceId ?? undefined, scope, offset, page)
      .then((next) => {
        setExtra((previous) => [...previous, next.text ?? ''])
        const nextOffset = next.next_offset ?? null
        const nextPage = next.next_page ?? null
        setCursor(nextOffset === null || nextPage === null ? null : { offset: nextOffset, page: nextPage })
      })
      .catch((cause: unknown) => {
        setMoreError(cause instanceof ApiError ? cause.message : '这一页没能读出来。')
      })
      .finally(() => { setLoadingMore(false) })
  }

  const copy = () => {
    if (body === '') return
    void navigator.clipboard?.writeText(body)
      .then(() => { setCopied(true); window.setTimeout(() => { setCopied(false) }, 1500) })
      .catch(() => { setCopied(false) })
  }

  const name = path.split(/[\\/]/).pop() ?? path
  const parent = path.slice(0, Math.max(0, path.length - name.length)).replace(/[\\/]$/, '')
  const lines = useMemo(() => (body === '' ? 1 : body.split('\n').length), [body])

  return (
    <section className={styles.pane} aria-label="文件预览">
      <header className={styles.bar}>
        <span className={styles.name} title={path}>{parent ? `${parent}/` : ''}{name}</span>
        <span className={styles.spacer} />
        {isText && rich ? <div className={styles.modeSwitch} role="group" aria-label="文件显示方式">
          <button aria-pressed={!source} onClick={() => setSource(false)}>预览</button>
          <button aria-pressed={source} onClick={() => setSource(true)}>源码</button>
        </div> : null}
        <button className={styles.tool} title="刷新文件" aria-label="刷新文件" onClick={() => {
          setExtra([]); setCursor(null); setRevision(value => value + 1); void first.refetch()
        }}>↻</button>
        {showCode ? <span className={styles.counter}>{lines} 行</span> : null}
        {showCode ? (
          <button type="button" className={styles.tool} aria-pressed={wrap}
            title={wrap ? '关闭自动换行' : '自动换行'} onClick={() => setWrap((value) => !value)}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <path d="M4 6h16M4 12h13a3 3 0 1 1 0 6h-3M4 18h4" />
            </svg>
          </button>
        ) : null}
        {isText ? (
          <button type="button" className={styles.tool} aria-label={copied ? '已复制' : hasMore ? '复制已加载内容' : '复制全文'} title={copied ? '已复制' : hasMore ? '复制已加载内容' : '复制全文'} onClick={copy}>
            <Copy size={14} />
          </button>
        ) : null}
        {scope === 'workspace' ? <OpenInAppMenu path={path} workspaceId={workspaceId} /> : null}
        <button type="button" className={styles.tool} title="关闭文件预览" aria-label="关闭文件预览" onClick={onClose}><Close size={14} /></button>
      </header>

      <div className={`${styles.body} ${wrap ? '' : styles.nowrap}`}>
        {first.isPending ? <p className={styles.hint}>正在读取…</p> : null}
        {first.isError ? (
          <div role="alert" className={styles.state}>
            <p className={styles.hint}>{first.error instanceof ApiError
              ? first.error.message : '读不到这个文件：它可能已被删除，或不在当前工作区内。'}</p>
            <Button size="sm" variant="ghost" onClick={() => void first.refetch()}>重试</Button>
          </div>
        ) : null}

        {data?.kind === 'image' ? <div className={styles.imageView}>
          <div className={styles.readerTools}>
            <button aria-label="缩小图片" onClick={() => setZoom(value => Math.max(.25, value - .25))}>−</button>
            <button onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
            <button aria-label="放大图片" onClick={() => setZoom(value => Math.min(4, value + .25))}>+</button>
          </div>
          <div className={styles.imageViewport}><img className={styles.image} style={{ width: `${zoom * 100}%`, maxWidth: 'none' }} src={data.url ?? undefined} alt={name} /></div>
        </div> : null}
        {data?.kind === 'document' && data.url ? <Suspense fallback={<p className={styles.hint}>正在加载文档阅读器…</p>}><DocumentView key={`${data.url}:${revision}`} url={data.url} /></Suspense> : null}
        {data?.kind === 'spreadsheet' ? <SpreadsheetView key={`${path}:${revision}`} path={path} workspaceId={workspaceId} scope={scope} /> : null}
        {isText && rich && !source ? (
          ['html', 'htm'].includes(extension)
            ? <iframe className={styles.htmlFrame} title="HTML 文件预览" sandbox="" srcDoc={'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:;">' + body} />
            : <article className={styles.markdown}><MarkdownMessage text={body} /></article>
        ) : null}

        {showCode ? (
          html !== null ? (
            <div className={`${styles.code} ${styles.highlight}`} dangerouslySetInnerHTML={{ __html: html }} />
          ) : (
            <PlainCode text={body} />
          )
        ) : null}

        {data?.kind === 'too_large' ? (
          <div className={styles.state}><p className={styles.hint}>
            图片 {formatSize(data.size_bytes)}，超过预览上限（2 MB）。
          </p></div>
        ) : null}
        {data?.kind === 'unreadable' ? (
          <div className={styles.state}><p className={styles.hint}>这个文件读不出来。</p></div>
        ) : null}
        {data?.kind === 'binary' ? (
          <div className={styles.state}><p className={styles.hint}>
            这是 .{data.suffix} 文件（{formatSize(data.size_bytes)}），二进制内容无法预览。
          </p></div>
        ) : null}
      </div>

      {isText ? (
        <footer className={styles.foot}>
          {data?.total_pages != null ? (
            <span className={styles.footNote}>第 {data.page ?? 1} / {data.total_pages} 页</span>
          ) : null}
          <span className={styles.footNote}>已显示 {body.length.toLocaleString()} 字符</span>
          {moreError !== null ? <span role="alert" className={styles.footError}>{moreError}</span> : null}
          {hasMore ? (
            <Button size="sm" variant="ghost" disabled={loadingMore} onClick={loadMore}>
              {loadingMore ? '正在加载…' : '加载更多'}
            </Button>
          ) : data?.beyond_limit === true ? (
            <span className={styles.footNote}>文件超过 4 MB，这里只能看到前 4 MB。</span>
          ) : (
            <span className={styles.footNote}>已到末尾</span>
          )}
        </footer>
      ) : null}
    </section>
  )
}

/** 没有高亮（纯文本 / 超长 / 语言不认识）时的渲染：自己分行，行号与高亮路径保持一致。 */
function PlainCode({ text }: { text: string }) {
  const lines = text.split('\n')
  return (
    <div className={styles.code}>
      <pre className={styles.plain}>
        {lines.map((line, index) => (
          <span className={styles.plainLine} key={index}>
            <span className={styles.gutter} aria-hidden="true">{index + 1}</span>
            <span className={styles.plainText}>{line}</span>
          </span>
        ))}
      </pre>
    </div>
  )
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${String(bytes)} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
