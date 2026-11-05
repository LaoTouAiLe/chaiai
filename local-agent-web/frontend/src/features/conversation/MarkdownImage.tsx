import { useContext, useState, type ComponentPropsWithoutRef } from 'react'
import { FileLinkContext } from './fileLinkContext'
import { filePathFromHref } from './fileLinks'
import { ImageLightbox } from './ImageLightbox'
import styles from './MarkdownImage.module.css'

/** Model-authored URLs are never requested before an explicit user click. */
export function MarkdownImage({ src, alt }: ComponentPropsWithoutRef<'img'>) {
  return <ImageContent key={typeof src === 'string' ? src : ''} src={src} alt={alt} />
}

function ImageContent({ src, alt }: ComponentPropsWithoutRef<'img'>) {
  const openFile = useContext(FileLinkContext)
  const [requested, setRequested] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const [zoomed, setZoomed] = useState(false)
  const url = typeof src === 'string' ? src : ''
  let hostname: string | null = null
  try { const parsed = new URL(url); if (['http:', 'https:'].includes(parsed.protocol)) hostname = parsed.hostname } catch { /* A local path or an invalid destination stays inert. */ }
  const remote = hostname !== null
  const path = remote ? null : filePathFromHref(url)
  const loaded = remote && requested === url
  if (loaded && !failed) return <span className={styles.figure}>
    <button type="button" className={styles.preview} aria-label={`放大图片：${alt || '正文图片'}`} onClick={() => setZoomed(true)}>
      <img src={url} alt={alt || '正文图片'} referrerPolicy="no-referrer" onError={() => setFailed(true)} />
    </button>
    {alt ? <span className={styles.caption}>{alt}</span> : null}
    {zoomed ? <ImageLightbox src={url} onClose={() => setZoomed(false)} /> : null}
  </span>
  return <span className={styles.placeholder}>
    <span className={styles.icon} aria-hidden="true">▧</span>
    <span className={styles.description}>
      <span>{failed ? `图片加载失败${alt ? `：${alt}` : ''}` : `[图片未自动加载${alt ? `：${alt}` : ''}]`}</span>
      {remote ? <small>{hostname}</small> : path ? <small title={path}>{path}</small> : null}
    </span>
    {remote ? <button type="button" onClick={() => { setFailed(false); setRequested(url) }}>{failed ? '重试加载图片' : '加载图片'}</button>
      : path && openFile ? <button type="button" onClick={() => openFile(path)}>查看图片</button> : null}
  </span>
}
