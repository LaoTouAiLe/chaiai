import { useState } from 'react'
import { ImageLightbox } from './ImageLightbox'
import styles from './Conversation.module.css'

function Thumbnail({ src, tiled, onOpen }: { src: string; tiled: boolean; onOpen: () => void }) {
  const [fit, setFit] = useState({ width: 240, height: 180, objectPosition: 'center', cropped: false })
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  return <button type="button" className={styles.userImageButton}
    data-image-variant={tiled ? 'tile' : 'single'} data-image-failed={failed || undefined}
    style={tiled ? undefined : { width: fit.width, aspectRatio: `${fit.width} / ${fit.height}` }}
    aria-label={failed ? '图片加载失败，点击重试' : '放大查看这张图片'}
    onClick={() => { if (failed) { setFailed(false); setAttempt(value => value + 1) } else onOpen() }}>
    {failed ? <span>图片加载失败<br />点击重试</span> : <img key={attempt} src={src} alt="随消息发送的图片" loading="lazy"
      style={{ objectPosition: fit.objectPosition, objectFit: fit.cropped ? 'cover' : 'scale-down' }} onError={() => setFailed(true)}
      onLoad={event => {
        // DSH MessageImage's singleFit rule (MIT): 240px long edge, bounded aspect ratio, no upscaling.
        const { naturalWidth: width, naturalHeight: height } = event.currentTarget
        if (!width || !height) return
        const natural = width / height
        const ratio = Math.min(4, Math.max(.25, natural))
        const box = ratio >= 1 ? { width: 240, height: 240 / ratio } : { width: 240 * ratio, height: 240 }
        const scale = Math.min(1, width / box.width, height / box.height)
        setFit({ width: Math.max(1, Math.round(box.width * scale)), height: Math.max(1, Math.round(box.height * scale)),
          objectPosition: natural < .25 ? 'center top' : natural > 4 ? 'left center' : 'center', cropped: natural < .25 || natural > 4 })
      }} />}
  </button>
}

export function MessageImages({ images }: { images: readonly string[] }) {
  const [zoomed, setZoomed] = useState<string | null>(null)
  if (images.length === 0) return null
  return <>
    <ul className={styles.userImages} aria-label="随这条消息发送的图片">
      {images.map((src, index) => <li key={`${index}-${src}`} className={styles.userImage}>
        <Thumbnail src={src} tiled={images.length > 1} onOpen={() => setZoomed(src)} />
      </li>)}
    </ul>
    {zoomed === null ? null : <ImageLightbox src={zoomed} onClose={() => setZoomed(null)} />}
  </>
}
