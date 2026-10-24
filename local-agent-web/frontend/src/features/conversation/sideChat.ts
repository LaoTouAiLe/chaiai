/**
 * 侧聊首轮注入的拆解（§21.7，2026-10-23）。
 *
 * 后端把主会话的问答作为前缀拼在**首轮提问**里（模型需要它），但 `question`
 * 因此存的是"一大段上下文 + 真正的问题"。直接显示的话，右侧对话里会躺着一整块
 * 8000 字符的上下文——用户看到的是噪音，不是自己问的那句话。
 *
 * 所以前端按同一个分界符拆开：上下文折进 `<details>`，问题照常显示。
 * **分界符必须与 `application/side_chat.py` 的 `INJECTION_MARKER` 逐字一致**。
 */

export const SIDE_CHAT_MARKER = '【以上为上下文，以下是我的问题】'

export interface SplitPrompt {
  /** 从主会话带过来的上下文；没有注入时为空串。 */
  context: string
  /** 用户真正问的那句。 */
  question: string
}

export function splitSideChatPrompt(text: string): SplitPrompt {
  const index = text.indexOf(SIDE_CHAT_MARKER)
  if (index === -1) return { context: '', question: text }
  return {
    context: text.slice(0, index).trim(),
    question: text.slice(index + SIDE_CHAT_MARKER.length).replace(/^\n+/, ''),
  }
}
