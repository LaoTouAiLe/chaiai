import { describe, expect, it } from 'vitest'

import type { components } from '../../api/schema.gen'
import {
  applyCompleted,
  applyDelta,
  reconcile,
  startTail,
  type StreamTail,
} from './streamTail'

type Snapshot = components['schemas']['SessionSnapshot']

const started = (messageId = 'a-1', role: 'user' | 'assistant' = 'assistant') => ({
  message_id: messageId,
  model_call_id: 'call-1',
  role,
  reset: false,
  chunk_sequence: 0,
})

const delta = (sequence: number, text: string, messageId = 'a-1') => ({
  message_id: messageId,
  model_call_id: 'call-1',
  role: 'assistant' as const,
  chunk_sequence: sequence,
  text_delta: text,
  channel: 'text' as const,
  reasoning_truncated: false,
})

const completed = (messageId = 'a-1') => ({
  message_id: messageId,
  model_call_id: 'call-1',
  role: 'assistant' as const,
  complete: true,
  text: '你好',
  reasoning: null,
  reasoning_truncated: false,
})

/** `reconcile` 只读 `messages`；其余字段与本用例无关，按真实契约的必填项造最小对象。 */
function snapshotWith(messages: unknown[]): Snapshot {
  return { messages } as unknown as Snapshot
}

describe('startTail', () => {
  it('ignores a boundary-overlapping start and only clears both channels for a newer reset', () => {
    const current = { messageId: 'a-1', modelCallId: 'call-1', text: '正文', reasoning: '思考', sequence: 4 }
    expect(startTail(current, started())).toEqual(current)
    const reset = { ...started(), reset: true, chunk_sequence: 5 }
    const cleared = startTail(current, reset)
    expect(cleared).toEqual({ messageId: 'a-1', modelCallId: 'call-1', text: '', sequence: 5 })
    const newer = applyDelta(cleared, delta(6, '重试正文')).tail
    expect(startTail(newer, reset)).toEqual(newer)
  })
  it('ignores a user message: only the assistant answer streams', () => {
    expect(startTail(null, started('u-1', 'user'))).toBeNull()
  })

  it('starts an empty tail for the assistant answer', () => {
    expect(startTail(null, started())).toEqual({
      messageId: 'a-1',
      modelCallId: 'call-1',
      text: '',
      sequence: 0,
    })
  })
})

describe('applyDelta', () => {
  it('reconstructs reasoning after reconnect and does not duplicate overlap or mix it into prose', () => {
    const snapshot = snapshotWith([{ kind: 'assistant', message_id: 'a-1', model_call_id: 'call-1',
      complete: false, text: '', reasoning: '正在分析。', chunk_sequence: 2, stream_channel: 'reasoning' }])
    const restored = reconcile(null, snapshot)
    expect(restored?.reasoning).toBe('正在分析。')
    expect(applyDelta(restored, { ...delta(2, '正在分析。'), channel: 'reasoning' }).duplicate).toBe(true)
    const thought = applyDelta(restored, { ...delta(3, '查看证据。'), channel: 'reasoning' }).tail
    const body = applyDelta(thought, delta(4, '这是回答。')).tail
    expect(body?.reasoning).toBe('正在分析。查看证据。')
    expect(body?.text).toBe('这是回答。')
    expect(body?.channel).toBe('text')
  })
  it('appends chunks in order', () => {
    const first = applyDelta(null, delta(1, '你'))
    const second = applyDelta(first.tail, delta(2, '好'))

    expect(second.tail?.text).toBe('你好')
    expect(second.gap).toBe(false)
  })

  it('drops a chunk that was already applied', () => {
    const applied = applyDelta(null, delta(1, '你')).tail

    // 重订阅会让同一轮的历史事件再投影一次：`model_call_id#chunk_sequence` 是稳定身份，
    // 同一块不能叠两遍（否则"你好"会变成"你你"）。
    const again = applyDelta(applied, delta(1, '你'))

    expect(again.duplicate).toBe(true)
    expect(again.tail?.text).toBe('你')
    expect(again.tail?.sequence).toBe(1)
  })

  it('reports a gap when a chunk is missing but still shows what arrived', () => {
    const applied = applyDelta(null, delta(1, '你')).tail

    const skipped = applyDelta(applied, delta(3, '吗'))

    expect(skipped.gap).toBe(true)
    expect(skipped.tail?.text).toBe('你吗')
  })
})

describe('applyCompleted', () => {
  it('drops the tail of the message that just completed', () => {
    const applied = applyDelta(startTail(null, started()), delta(1, '你')).tail

    expect(applyCompleted(applied, completed())).toBeNull()
  })

  it('keeps a tail belonging to a different message', () => {
    const applied: StreamTail = { messageId: 'a-2', modelCallId: 'call-2', text: 'x', sequence: 1 }

    expect(applyCompleted(applied, completed('a-1'))).toEqual(applied)
  })
})

describe('reconcile', () => {
  it('drops the tail once the snapshot carries that message as complete', () => {
    const applied: StreamTail = { messageId: 'a-1', modelCallId: 'call-1', text: '你', sequence: 1 }

    // 停止条件"临时 delta 与最终消息不得重复显示"：快照里已经是完整消息了，尾部必须让位。
    const snapshot = snapshotWith([{ kind: 'assistant', message_id: 'a-1', text: '你好', complete: true }])

    expect(reconcile(applied, snapshot)).toBeNull()
  })

  it('keeps the tail while the snapshot still shows it as incomplete', () => {
    const applied: StreamTail = { messageId: 'a-1', modelCallId: 'call-1', text: '你', sequence: 1 }

    const snapshot = snapshotWith([{ kind: 'assistant', message_id: 'a-1', text: '', complete: false }])

    expect(reconcile(applied, snapshot)).toEqual(applied)
  })

  it('drops an orphan tail the snapshot does not know about', () => {
    const applied: StreamTail = { messageId: 'a-9', modelCallId: null, text: '旧', sequence: 3 }

    expect(reconcile(applied, snapshotWith([]))).toBeNull()
  })
})
