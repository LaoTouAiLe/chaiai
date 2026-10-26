/** 只解析 Markdown 链接目标；不扫描正文，也不放开任意 URL 协议。 */
export function filePathFromHref(href: string): string | null {
  let value = href.trim()
  if (!value || value.startsWith('#') || /^(?:https?|mailto|ircs?|xmpp):/i.test(value) || /^[\\/]{2}/.test(value)) return null
  if (/^file:/i.test(value)) {
    try {
      const url = new URL(value)
      if (url.hostname && url.hostname !== 'localhost') return null
      value = url.pathname
    } catch { return null }
  } else if (/^[a-z][a-z\d+.-]*:/i.test(value) && !/^[a-z]:(?:[\\/]|%2f|%5c)/i.test(value)) {
    return null
  }
  value = value.split(/[?#]/, 1)[0]
  try { value = decodeURIComponent(value) } catch { /* 字面量百分号仍可作为文件名。 */ }
  value = value.replace(/\\/g, '/').replace(/^\/([a-z]:\/)/i, '$1')
  value = value.replace(/:\d+(?::\d+)?$/, '')
  return normalizeFilePath(value)
}

/** 输入已是文件系统路径，保留 #、% 等文件名字符，不再按 URL 解码。 */
function normalizeFilePath(value: string): string | null {
  value = value.replace(/\\/g, '/').replace(/^\/([a-z]:\/)/i, '$1')
  if (!value || [...value].some(char => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
    || value.endsWith('/') || value.startsWith('//')) return null
  if (/^[a-z][a-z\d+.-]*:/i.test(value) && !/^[a-z]:\//i.test(value)) return null
  const drive = /^[a-z]:\//i.exec(value)?.[0]
  const prefix = drive ? `${drive[0].toUpperCase()}:/` : value.startsWith('/') ? '/' : ''
  const parts: string[] = []
  for (const part of value.slice(drive ? 3 : prefix.length).split('/')) {
    if (!part || part === '.') continue
    if (part === '..' && parts.length && parts.at(-1) !== '..') parts.pop()
    else if (part === '..' && prefix) continue
    else parts.push(part)
  }
  return parts.length && parts.at(-1) !== '..' ? prefix + parts.join('/') : null
}

export function sameFilePath(first: string | null, second: string): boolean {
  if (first === null) return false
  const a = normalizeFilePath(first)
  const b = normalizeFilePath(second)
  return a !== null && b !== null && (/^[a-z]:\//i.test(a) ? a.toLowerCase() === b.toLowerCase() : a === b)
}
