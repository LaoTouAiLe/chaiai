/** 地址栏接受无协议域名（含端口）；本机回环地址默认 HTTP，其他域名默认 HTTPS。 */
export function browserAddress(raw: string): string {
  let value = raw.trim()
  if (value === 'about:blank') return value
  const hostWithPort = /^(?:[^\s/?#:@]+\.[^\s/?#:@]+|\[[\da-f:.]+\]):\d+(?:[/?#]|$)/i.test(value)
  if (/^(localhost|127(?:\.\d+){3}|\[::1\])(?::\d+)?(?:[/?#]|$)/i.test(value)) value = `http://${value}`
  else if (hostWithPort || !/^[a-z][a-z\d+.-]*:/i.test(value)) value = `https://${value}`
  const url = new URL(value)
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('请输入 http/https 网址，地址中不要包含账号密码。')
  return url.href
}
