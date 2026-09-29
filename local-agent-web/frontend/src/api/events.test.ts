import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from './client'
import { SseFrameParser, readSessionEvents, type SessionStreamHandlers, type SseFrame } from './events'

function sseResponse(text: string, status = 200): Response {
  return new Response(text, {
    status,
    headers: { 'content-type': 'text/event-stream' },
  })
}

function collecting() {
  const events: unknown[] = []
  const notices: string[] = []
  const malformed: SseFrame[] = []
  const handlers: SessionStreamHandlers = {
    onEvent: (event) => events.push(event),
    onNotice: (name) => notices.push(name),
    onMalformed: (frame) => malformed.push(frame),
  }
  return { events, notices, malformed, handlers }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SseFrameParser', () => {
  it('keeps a frame that arrives split across chunks', () => {
    const parser = new SseFrameParser()

    // 实测过网络会把一帧切在两行之间：按 `\n\n` 一次性切就会丢帧。
    expect(parser.push('event: message\nda')).toEqual([])
    expect(parser.push('ta: {"event_type":"task_completed"}\n\n')).toEqual([
      { event: 'message', data: '{"event_type":"task_completed"}' },
    ])
  })

  it('reads several frames out of one chunk and tolerates CRLF', () => {
    const parser = new SseFrameParser()

    const frames = parser.push('data: one\r\n\r\ndata: two\n\n')

    expect(frames).toEqual([
      { event: 'message', data: 'one' },
      { event: 'message', data: 'two' },
    ])
  })

  it('ignores comments and heartbeats', () => {
    const parser = new SseFrameParser()

    expect(parser.push(': ping\n\n')).toEqual([])
  })

  it('joins multi-line data and does not emit an unfinished frame', () => {
    const parser = new SseFrameParser()

    expect(parser.push('data: {"a":\ndata: 1}')).toEqual([])
    expect(parser.push('\n\n')).toEqual([{ event: 'message', data: '{"a":\n1}' }])
  })

  it('ignores the id field instead of promising a resume cursor', () => {
    const parser = new SseFrameParser()

    // 契约 §15.6：v0.4 不发 `id:`；真出现了也不该被当成游标记住。
    expect(parser.push('id: 42\ndata: x\n\n')).toEqual([{ event: 'message', data: 'x' }])
  })
})

describe('readSessionEvents', () => {
  it('requests the session stream and dispatches events by event_type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse(
        'event: message\ndata: {"event_type":"message_delta","payload":{"text_delta":"你"}}\n\n' +
          'event: stream_overrun\ndata: {"dropped":3}\n\n',
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { events, notices, handlers } = collecting()

    await readSessionEvents('s-1', handlers)

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/sessions/s-1/events')
    expect(events).toEqual([
      { event_type: 'message_delta', payload: { text_delta: '你' } },
    ])
    // 传输层帧交给调用方，不当成事件类型。
    expect(notices).toEqual(['stream_overrun'])
  })

  it('dispatches the real server named business frames instead of misclassifying them as notices', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse(
      'event: session_snapshot\ndata: {"event_type":"session_snapshot","payload":{"session_id":"s-1"}}\n\n' +
      'event: message_delta\ndata: {"event_type":"message_delta","payload":{"text_delta":"你"}}\n\n' +
      'event: message_completed\ndata: {"event_type":"message_completed","payload":{"message_id":"a-1"}}\n\n',
    )))
    const { events, notices, handlers } = collecting()

    await readSessionEvents('s-1', handlers)

    expect(events.map((event) => (event as { event_type: string }).event_type))
      .toEqual(['session_snapshot', 'message_delta', 'message_completed'])
    expect(notices).toEqual([])
  })

  it('rejects a named frame whose name disagrees with its envelope', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse(
      'event: message_delta\ndata: {"event_type":"task_completed"}\n\n',
    )))
    const { events, malformed, handlers } = collecting()
    await readSessionEvents('s-1', handlers)
    expect(events).toEqual([])
    expect(malformed).toHaveLength(1)
  })

  it('reports a frame that is not a JSON envelope instead of dropping it silently', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseResponse('data: not-json\n\ndata: {"no_event_type":1}\n\n')),
    )
    const { events, malformed, handlers } = collecting()

    await readSessionEvents('s-1', handlers)

    expect(events).toEqual([])
    expect(malformed).toHaveLength(2)
  })

  it('turns a rejected subscription into an ApiError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { code: 'SESSION_NOT_FOUND', message: '会话不存在' } }), {
          status: 404,
          headers: { 'content-type': 'application/json' },
        }),
      ),
    )
    const { handlers } = collecting()

    await expect(readSessionEvents('s-nope', handlers)).rejects.toBeInstanceOf(ApiError)
  })

  it('fails loudly when the response claims to be a stream but has no body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 200 })))
    const { handlers } = collecting()

    await expect(readSessionEvents('s-1', handlers)).rejects.toThrow('事件流没有响应体')
  })
})
