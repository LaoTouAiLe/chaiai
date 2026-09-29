// 流式"临时尾部"（T30）。
//
// 契约（计划 §6.3）：`session_snapshot` 是**权威可恢复状态**，`message_delta` 只更新"正在生成的那条"，
// `message_completed` 到达后按稳定 `message_id` 对账。T30 的三条停止条件直接落成这里的规则：
// 1. 前端不得靠读本地 State 恢复 → 恢复一律来自快照，尾部只是"还没进快照的那一段"；
// 2. 流式失败不得重新提交用户请求 → 这里全是纯函数，丢尾部不发起任何请求；
// 3. 临时 delta 与最终消息不得重复显示 → 尾部在 `message_completed` 到达、或快照里已经出现
//    这条**完整**消息时**立刻丢弃**。
//
// 纯函数写在这里（而不是埋在组件的 useState 里）是因为这三条都是判断分支，必须能单独测。

import type { components } from '../../api/schema.gen'

type MessageStartedPayload = components['schemas']['MessageStartedPayload']
type MessageDeltaPayload = components['schemas']['MessageDeltaPayload']
type MessageCompletedPayload = components['schemas']['MessageCompletedPayload']
type SessionSnapshot = components['schemas']['SessionSnapshot']

export interface StreamTail {
  readonly messageId: string
  readonly modelCallId: string | null
  /** 已收到的增量拼起来的文本（**不含**快照里已有的内容）。 */
  readonly text: string
  /** 已应用的最大 `chunk_sequence`；同一 `model_call_id` 内递增。 */
  readonly sequence: number
}

/** 增量的处理结果：`duplicate` 与 `gap` 由调用方决定要不要提示/刷新。 */
export interface DeltaEffect {
  readonly tail: StreamTail | null
  /** 这一块已经应用过（重复投递）。 */
  readonly duplicate: boolean
  /** 中间缺了块：文本仍可用，但快照才是补齐的来源。 */
  readonly gap: boolean
}

/** `message_started`：开始一条尾部。只跟助手消息——用户消息不流式。 */
export function startTail(
  current: StreamTail | null,
  payload: MessageStartedPayload,
): StreamTail | null {
  if (payload.role !== 'assistant') return current
  return {
    messageId: payload.message_id,
    modelCallId: payload.model_call_id,
    text: '',
    sequence: 0,
  }
}

export function applyDelta(current: StreamTail | null, payload: MessageDeltaPayload): DeltaEffect {
  if (payload.role !== 'assistant') return { tail: current, duplicate: false, gap: false }
  const base = current !== null && current.messageId === payload.message_id ? current : null

  if (base !== null && payload.chunk_sequence <= base.sequence) {
    // `model_call_id#chunk_sequence` 是增量的**稳定身份**（契约 §15.6）。重订阅会让同一轮的历史
    // 事件再投影一次，所以重复投递是常态：按身份丢弃，不能让同一段文字叠两遍。
    return { tail: base, duplicate: true, gap: false }
  }

  const expected = (base === null ? 0 : base.sequence) + 1
  const gap = payload.chunk_sequence > expected
  return {
    // 缺块时也不猜中间内容：把服务端给到的这一段如实拼上，由调用方用快照补齐。
    tail: {
      messageId: payload.message_id,
      modelCallId: payload.model_call_id,
      text: (base === null ? '' : base.text) + payload.text_delta,
      sequence: payload.chunk_sequence,
    },
    duplicate: false,
    gap,
  }
}

/** `message_completed`：这条消息从此由快照负责，尾部立刻收掉（否则会重复显示）。 */
export function applyCompleted(
  current: StreamTail | null,
  payload: MessageCompletedPayload,
): StreamTail | null {
  if (current !== null && current.messageId === payload.message_id) return null
  return current
}

/**
 * 用权威快照对账尾部。
 *
 * 两种丢弃：① 快照里已有这条**完整**消息（说明它已经进权威状态了）；② 快照里根本没有这条消息，
 * 但尾部是更早一轮留下的孤儿（例如切会话回来）。保留的条件只有一个：这个 `message_id` 在快照里
 * 是"还没完成的那条"。
 */
export function reconcile(current: StreamTail | null, snapshot: SessionSnapshot): StreamTail | null {
  if (current === null) return null
  // 生成的类型把 `messages` 标成可选（契约里它必有），这里按"没有消息"处理即可：
  // 那种情况下尾部就是孤儿，本来就该丢。
  const messages = snapshot.messages ?? []
  const found = messages.find((message) => message.message_id === current.messageId)
  if (found === undefined) return null
  if (found.kind === 'assistant' && found.complete) return null
  return current
}
