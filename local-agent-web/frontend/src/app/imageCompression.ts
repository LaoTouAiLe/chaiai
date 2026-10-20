/**
 * 上传前的图片压缩（2026-10-20）。
 *
 * 为什么必须在**上传前**做：附件会以 `data:` URL 进消息、随请求体发给模型。实测一张 3.9 MB
 * 的截图 base64 后是 5.1 MB 文本，DeepSeek 直接回 400（`OpenAIInvalidRequestError`，页面显示
 * "本轮模型调用失败"）。服务端另有 2 MiB 兜底上限，但那只在绕过页面时生效——正常路径应当在
 * 这里就把图压到模型吃得下的尺寸。
 *
 * 取舍：长边 > 4096px 或 > 800 KB 才动它；压成 JPEG（截图文字在 0.85 质量下仍清晰），透明区域
 * 填白（JPEG 不支持 alpha）。GIF 不重编码：canvas 会丢掉动画，超限时直接给出可执行的拒绝。
 *
 * **2026-10-20 从 1600 / 800 KB 放宽**：长截图压到 1600 后字太小，模型会说"看不清"，然后自己
 * 去翻磁盘上的原图（`view_image`），反而更容易触发别的问题。端点上限是单边 8192px / 32 MiB，
 * 4096 仍留了一倍余量、字也清楚得多。
 */

/** 长边上限（像素）。端点硬上限是 8192，这里取一半，兼顾清晰度与体积。 */
export const MAX_EDGE = 4096
/** 低于这个体积且尺寸合规的图原样上传，不重编码（避免无谓的画质损失）。 */
export const PASSTHROUGH_BYTES = 800 * 1024
/** 压缩目标体积。低于服务端 4 MiB 兜底，余量给 base64 膨胀（约 1.33 倍）。 */
export const TARGET_BYTES = 2 * 1024 * 1024
/** 与服务端 `MAX_UPLOAD_IMAGE_BYTES` 同一条口径（只用于不重编码格式的提前拒绝）。 */
export const MAX_UPLOAD_BYTES = 4 * 1024 * 1024

export interface PreparedImage {
  /** 不含 `data:` 前缀的 base64 —— 上传接口要的就是它。 */
  base64: string
  /** 真正要上传的字节（预览用同一个）。 */
  blob: Blob
  /** 服务端仍会按魔数复核，这里只是预览与调试可见。 */
  mediaType: string
  /** 是否发生了重编码（页面据此决定要不要提示"已压缩"）。 */
  compressed: boolean
}

/** 等比缩放到长边不超过 `maxEdge`；不放大。 */
export function computeTargetSize(
  width: number,
  height: number,
  maxEdge: number = MAX_EDGE,
): { width: number; height: number } {
  const longest = Math.max(width, height)
  if (longest <= maxEdge) {
    return { width: Math.max(1, Math.round(width)), height: Math.max(1, Math.round(height)) }
  }
  const scale = maxEdge / longest
  return { width: Math.max(1, Math.round(width * scale)), height: Math.max(1, Math.round(height * scale)) }
}

/** 把一张图准备成可上传的形态：该压的压，不该动的原样返回。 */
export async function prepareImageForUpload(file: File): Promise<PreparedImage> {
  const unchanged = async (reason: string | null): Promise<PreparedImage> => {
    if (reason !== null) throw new Error(reason)
    return {
      base64: await readAsBase64(file),
      blob: file,
      mediaType: file.type,
      compressed: false,
    }
  }

  // GIF：重编码会丢动画。体积在限内就原样发，超限就明确拒绝，不偷偷换成一个静帧。
  if (file.type === 'image/gif') {
    return unchanged(
      file.size > MAX_UPLOAD_BYTES
        ? `这张 GIF 有 ${Math.round(file.size / 1024)} KB，超过 ${MAX_UPLOAD_BYTES / 1024 / 1024} MB 上限：` +
            '请先裁剪或转成静态图再上传（压缩会丢掉动画，所以这里不替你转）。'
        : null,
    )
  }

  // 小图且尺寸合规：原样上传，不重编码。
  if (file.size <= PASSTHROUGH_BYTES && file.type !== 'image/webp') {
    const size = await readSize(file)
    if (Math.max(size.width, size.height) <= MAX_EDGE) return unchanged(null)
  }

  const image = await loadImage(file)
  try {
    const target = computeTargetSize(image.width, image.height)
    const canvas = document.createElement('canvas')
    canvas.width = target.width
    canvas.height = target.height
    const context = canvas.getContext('2d')
    if (context === null) throw new Error('这个浏览器无法压缩图片：请换一张更小的图再试。')
    // JPEG 没有透明通道：先铺白底，否则 PNG 的透明区域会变成黑色。
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, target.width, target.height)
    context.drawImage(image.source, 0, 0, target.width, target.height)

    const blob = await encodeWithinBudget(canvas)
    if (blob.size > MAX_UPLOAD_BYTES) {
      throw new Error(
        `这张图压缩后仍有 ${Math.round(blob.size / 1024)} KB（上限 ${MAX_UPLOAD_BYTES / 1024 / 1024} MB）：` +
          '请把长边缩到 1600px 以内，或截取需要的那一块再上传。',
      )
    }
    return { base64: await readAsBase64(blob), blob, mediaType: 'image/jpeg', compressed: true }
  } finally {
    image.close()
  }
}

/** 逐档降质，取第一个落在目标体积内的结果；都超就返回最低一档（调用方还会查上限）。 */
async function encodeWithinBudget(canvas: HTMLCanvasElement): Promise<Blob> {
  const qualities = [0.85, 0.75, 0.65, 0.55]
  let last: Blob | null = null
  for (const quality of qualities) {
    const blob = await toJpeg(canvas, quality)
    if (blob === null) break
    last = blob
    if (blob.size <= TARGET_BYTES) return blob
  }
  if (last === null) throw new Error('这张图无法压缩成 JPEG：请换一张图再试。')
  return last
}

function toJpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality))
}

async function readAsBase64(blob: Blob): Promise<string> {
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = String(reader.result ?? '')
      const comma = result.indexOf(',')
      resolve(comma === -1 ? result : result.slice(comma + 1))
    }
    reader.onerror = () => reject(new Error('这张图读不出来，换一张试试'))
    reader.readAsDataURL(blob)
  })
}

async function readSize(file: File): Promise<{ width: number; height: number }> {
  const image = await loadImage(file)
  try {
    return { width: image.width, height: image.height }
  } finally {
    image.close()
  }
}

interface LoadedImage {
  source: CanvasImageSource
  width: number
  height: number
  close: () => void
}

async function loadImage(file: File): Promise<LoadedImage> {
  if (typeof createImageBitmap === 'function') {
    const bitmap = await createImageBitmap(file)
    return {
      source: bitmap,
      width: bitmap.width,
      height: bitmap.height,
      close: () => bitmap.close(),
    }
  }
  const url = URL.createObjectURL(file)
  try {
    const element = await new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image()
      image.onload = () => resolve(image)
      image.onerror = () => reject(new Error('这张图读不出来，换一张试试'))
      image.src = url
    })
    return {
      source: element,
      width: element.naturalWidth,
      height: element.naturalHeight,
      close: () => undefined,
    }
  } finally {
    URL.revokeObjectURL(url)
  }
}
