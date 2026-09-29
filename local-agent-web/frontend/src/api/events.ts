// 会话事件流（SSE）的读取与解析（T30）。
//
// 契约要点（ARCHITECTURE §15.6、计划 §6.2），三条都直接决定了这里的写法：
//
// 1. **不发 `id:`**：v0.4 没有持久游标，"断线重连"的语义是**先取快照再订阅**，不是续传。
//    所以这里既忽略 `id:` 字段，也不用 `EventSource`——它会在重连时自动带上 `Last-Event-ID`，
//    那等于向服务端承诺一个不存在的续传能力。
// 2. **增量只是临时尾部**：`message_delta` 只更新"正在生成的那条"，`message_completed` 到达后
//    按稳定 `message_id` 与快照对账（见 `useSessionStream`）；文本的权威来源始终是 `session_snapshot`。
// 3. **传输层帧不是事件类型**：`stream_overrun` / `stream_idle_timeout` 是连接被收尾时的提示帧，
//    不在 `event_type` 枚举里，也不代表数据损坏——如实交给调用方，不假装没发生。

import { ApiError, apiErrorFromResponse } from './client'
import type { components } from './schema.gen'

/** 事件信封（判别字段是 `event_type`）；类型来自受控 OpenAPI，不手写。 */
export type EventEnvelope = components['schemas']['EventEnvelope']
export type MessageDeltaPayload = components['schemas']['MessageDeltaPayload']
export type SessionSnapshotEvent = components['schemas']['SessionSnapshotEvent']

/** 一帧 SSE：帧名（缺省是 `message`）与原始 `data` 文本。 */
export interface SseFrame {
  event: string
  data: string
}

/**
 * 增量解析 SSE 文本。
 *
 * 为什么要有状态：网络分片会把一帧切成任意片段（实测过一帧被切在两行之间），
 * 所以"按 `\n\n` 切"这种一次性的解析会丢帧。
 */
export class SseFrameParser {
  private buffer = ''
  private eventName = ''
  private dataLines: string[] = []

  /** 喂入一段文本，返回这一段里**完整**的帧；不完整的部分留在内部缓冲里。 */
  push(chunk: string): SseFrame[] {
    this.buffer += chunk
    const frames: SseFrame[] = []
    let breakIndex = this.buffer.indexOf('\n')
    while (breakIndex !== -1) {
      const line = this.buffer.slice(0, breakIndex).replace(/\r$/, '')
      this.buffer = this.buffer.slice(breakIndex + 1)
      this.consumeLine(line, frames)
      breakIndex = this.buffer.indexOf('\n')
    }
    return frames
  }

  private consumeLine(line: string, frames: SseFrame[]): void {
    if (line === '') {
      // 空行 = 一帧结束。只有收到过 `data` 才算帧：注释与心跳（`: ping`）不算。
      if (this.dataLines.length > 0) {
        frames.push({ event: this.eventName === '' ? 'message' : this.eventName, data: this.dataLines.join('\n') })
      }
      this.eventName = ''
      this.dataLines = []
      return
    }
    if (line.startsWith(':')) return // 注释/心跳
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'data') {
      this.dataLines.push(value)
    } else if (field === 'event') {
      this.eventName = value
    }
    // `id:` / `retry:` 刻意忽略：v0.4 无持久游标，重连走"先取快照"（§15.6）。
  }
}

export interface SessionStreamHandlers {
  /** 一个已解析的事件（`event_type` 已确认存在）。 */
  onEvent: (event: EventEnvelope) => void
  /** 传输层帧名（含未知帧）：调用方决定提示什么，读取层不替它决定。 */
  onNotice?: (name: string) => void
  /** 帧里的 `data` 不是合法 JSON 或缺 `event_type`：如实上报，不静默吞掉。 */
  onMalformed?: (frame: SseFrame, error: unknown) => void
}

function isEventEnvelope(value: unknown): value is EventEnvelope {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { event_type?: unknown }).event_type === 'string'
  )
}

function dispatch(frame: SseFrame, handlers: SessionStreamHandlers): void {
  // 服务端把业务 event_type 同时写在 SSE 的 event: 行与 JSON 信封里。
  // 只有这两种传输层收尾帧不是业务事件；把所有具名帧当 notice 会吞掉全部真实增量。
  if (frame.event === 'stream_overrun' || frame.event === 'stream_idle_timeout') {
    handlers.onNotice?.(frame.event)
    return
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(frame.data)
    if (!isEventEnvelope(parsed)) throw new Error('事件缺少 event_type')
    if (frame.event !== 'message' && frame.event !== parsed.event_type) {
      throw new Error('SSE 帧名与事件信封不一致')
    }
  } catch (error) {
    handlers.onMalformed?.(frame, error)
    return
  }
  handlers.onEvent(parsed)
}

/**
 * 连上某个会话的事件流，一直读到连接结束（或 `signal` 中止）。
 *
 * 这个函数**只负责读**：不重连、不重发、不合并事件。重连策略由 `useSessionStream` 决定，
 * 因为"重连前先取快照"是它的职责（读层不该偷偷补发请求）。
 */
export async function readSessionEvents(
  sessionId: string,
  handlers: SessionStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/events`, {
    headers: { accept: 'text/event-stream' },
    signal,
  })
  if (!response.ok) throw await apiErrorFromResponse(response)
  if (response.body === null) {
    throw new ApiError({
      status: response.status,
      code: null,
      message: '事件流没有响应体',
      detail: '响应头声明了事件流，但读取不到内容；多半是中间层把它整体缓冲或截断了。',
    })
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SseFrameParser()
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) {
        dispatch(frame, handlers)
      }
    }
  } finally {
    // 主动收尾：`abort` 时读端要归还锁，否则连接会挂到服务端空闲超时才断。
    reader.releaseLock()
  }
}
