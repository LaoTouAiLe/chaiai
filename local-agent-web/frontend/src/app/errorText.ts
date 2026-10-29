// 错误 → 一句给用户看的话（2026-10-28）。
//
// **文案的真源在后端**：`ErrorView.message` / `hint` 本来就是面向用户的措辞（§15.5 要求错误
// DTO 区分"用户可执行提示"）。页面**不再维护"错误码 → 中文"的表**——那张表迟早与后端分叉，
// 后端新增一个码时前端永远慢一拍，而且同一个错误会在两处被写成两种说法。
//
// 这里只保留两类本地判断，都不是"文案"：
// 1. 传输层失败（不是 `ApiError`）→ 说"本机服务没有响应"；
// 2. **安全兜底**：后端 message 万一混进密钥样式串或 URL（供应商回显），退回一句通用说法，
//    而不是把可疑内容摆到输入区上方。
//
// 已知取舍：`URL_LIKE` 也会拦下"故意给链接"的文案（例如将来的升级提示）。目前后端没有任何
// 错误文案带链接；真要带，得给这条兜底加白名单，而不是放宽成"允许一切 URL"。

import { ApiError } from '../api/client'

/** 与投影层同一类密钥样式（`_KEY_LIKE`）：出现在用户文案里就说明后端漏了脱敏。 */
const SECRET_LIKE = /\bsk-[A-Za-z0-9_-]{8,}/
const URL_LIKE = /https?:\/\/\S+/i
const FALLBACK = '服务端没有接受这次操作'

/**
 * 展示一段后端错误文案；`withHint` 为真时把可执行提示拼在后面（审批这类需要"下一步"的地方）。
 * 传进来的不是 `ApiError` 时按传输层失败处理。
 */
export function backendErrorText(error: unknown, options: { withHint?: boolean } = {}): string {
  if (!(error instanceof ApiError)) return '本机服务没有响应'
  const message = error.message.trim()
  const hint = (error.hint ?? '').trim()
  const text = options.withHint === true && hint !== '' && !message.includes(hint)
    ? `${message} ${hint}`
    : message
  if (text === '' || SECRET_LIKE.test(text) || URL_LIKE.test(text)) return FALLBACK
  return text
}
