// 追问的引用块（2026-10-26）。
//
// 为什么拼进用户消息正文、而不是做成结构化字段：
//
//   引用落在**上下文末尾**（当前这条用户消息），前面的历史前缀一字不变，所以不影响提示缓存的
//   命中——缓存判定的是"前缀逐字相同"。要做成结构化引用（后端能区分"引用"与"问题"、单独高亮
//   或统计），得改 API 契约、持久化与投影三处，那是另一件事。
//
// 代价要说清：引用会成为用户消息的一部分进入历史，之后每轮都带着它。所以**整条回答**那个入口
// 有长度上限——那段文字本来就在上文，引用只起"指向"作用，复制全文等于让同一段内容进两次上下文。
// 拖选入口不截断：长度由用户自己控制，通常只有几十到几百字。

/** 整条回答被引用时的上限（字符）。 */
export const QUOTE_LIMIT = 800

/** 输入框上方那条引用只显示单行，超出用省略号。 */
export function quotePreview(text: string, limit = 120): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > limit ? `${flat.slice(0, limit)}…` : flat
}

/**
 * 引用块：逐行加 `> `，空行写成 `>`，保证 Markdown 引用块结构不被空行截断。
 *
 * 用引用块而不是自造的 `【引用】` 前缀：这是模型在训练语料里见惯的"这段是转述"的标记，
 * 不需要额外解释也能读懂边界。
 */
export function quoteMarkdown(text: string, limit = QUOTE_LIMIT): string {
  const trimmed = text.trim()
  const clipped = trimmed.length > limit
  const body = clipped ? trimmed.slice(0, limit) : trimmed
  const lines = body.split('\n')
  if (clipped) lines.push('…（引用已截断，完整内容见原文）')
  return lines.map((line) => (line.trim() === '' ? '>' : `> ${line}`)).join('\n')
}

/** 最终交给模型的消息正文：引用块 + 空行 + 用户问题。没有引用时原样返回草稿。 */
export function composeQuotedMessage(quote: string | null | undefined, draft: string): string {
  if (quote === null || quote === undefined || quote.trim() === '') return draft
  const question = draft.trim()
  return question === '' ? quoteMarkdown(quote) : `${quoteMarkdown(quote)}\n\n${question}`
}
