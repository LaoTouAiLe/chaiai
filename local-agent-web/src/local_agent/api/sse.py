"""会话级 SSE（T29；契约 §15.3、§6.2、§6.3）。

- **先快照、再增量**：订阅时总线先投递由当前 Checkpoint 投影的 `session_snapshot`，其后才是连接
  建立之后的实时事件（顺序由 `EventBus.subscribe` 保证，见 `application/service.py`）。
- **不做持久游标**：不发 `id:`。SSE 的 `id:` 会被浏览器当作 `Last-Event-ID` 续传凭据，而 v0.4
  明确不做 `after_cursor`——发了就等于承诺一段不存在的补发能力。重连靠**新快照**。
- **心跳用注释行**（`:` 开头）：它不是事件，不会污染客户端的事件流。
- **空闲上限**：超过 `web.sse_idle_timeout_seconds` 没有任何事件就结束这条连接（**不取消任务**）。
- **慢消费者**：单次排空待发事件超过 `web.sse_max_pending_events` 即判定落后，发一条
  `stream_overrun` 后关闭。客户端重连会拿到新快照，而临时增量本来就不保证补发（§6.2）。

为什么用轮询而不是条件变量：`EventSubscription` 的队列是被**任意线程**（跑图的那个）push 的，
跨线程唤醒 asyncio 需要一个线程安全的信号通道；本机单用户下 50ms 轮询的代价远小于把
`threading.Event` 嫁接到事件循环里——而且那会引入新一轮并发问题（T28 已经在这类问题上栽过一次）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final

from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from local_agent.api.container import ApiRuntime
from local_agent.api.schemas import ErrorResponse
from local_agent.application.service import EventSubscription

SSE_MEDIA_TYPE: Final[str] = "text/event-stream"
#: 断线重连提示（毫秒）。重连会拿到新快照，因此这里不需要退避策略。
RETRY_HINT_MS: Final[int] = 3000
#: 心跳：注释行，不是事件。
HEARTBEAT_FRAME: Final[str] = ": heartbeat\n\n"
#: 两种结束原因（都不是业务事件，只是告诉客户端"请重连"）。
OVERRUN_EVENT: Final[str] = "stream_overrun"
IDLE_EVENT: Final[str] = "stream_idle_timeout"
#: 轮询间隔（秒）：见模块说明里的取舍。
POLL_SECONDS: Final[float] = 0.05


def _frame(event_type: str, payload: Mapping[str, Any]) -> str:
    data = json.dumps(dict(payload), ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n"


def format_event(event: Any) -> str:
    """一个事件帧：SSE 事件名 = `event_type`，`data` 是**完整信封**的 JSON。

    发完整信封（而不是只发 payload）是为了让前端能按生成的 TS 类型直接 `JSON.parse` 成
    `EventEnvelope`：判别字段、`stream_sequence` 与 `run_id` 都在里面（§15.4）。
    """
    return _frame(event.event_type, jsonable_encoder(event))


async def event_stream(runtime: ApiRuntime, subscription: EventSubscription) -> AsyncIterator[str]:
    """把订阅队列泵成 SSE 帧，直到空闲超时、被判定落后、或订阅被关闭。"""
    web = runtime.web
    loop = asyncio.get_running_loop()
    last_activity = loop.time()
    last_heartbeat = last_activity

    yield f"retry: {RETRY_HINT_MS}\n\n"
    while True:
        # 溢出标记来自**入队时**的封顶（生产者视角），不只依赖排出这一批的长度：
        # 只看这一批的话，客户端停读期间队列可以在两次轮询之间无限增长。
        if subscription.overflowed:
            yield _frame(
                OVERRUN_EVENT,
                {
                    "pending": subscription.pending_count,
                    "limit": subscription.max_pending,
                    "detail": "客户端落后太多：已放弃这次订阅，请重连（重连会先收到新的会话快照）。",
                },
            )
            return

        pending = subscription.poll()
        now = loop.time()
        if pending:
            if len(pending) > web.sse_max_pending_events:
                yield _frame(
                    OVERRUN_EVENT,
                    {
                        "pending": len(pending),
                        "limit": web.sse_max_pending_events,
                        "detail": "客户端落后太多：已放弃这次订阅，请重连（重连会先收到新的会话快照）。",
                    },
                )
                return
            for event in pending:
                yield format_event(event)
            last_activity = now
            last_heartbeat = now
            continue
        if subscription.closed:
            return
        if now - last_activity >= web.sse_idle_timeout_seconds:
            yield _frame(
                IDLE_EVENT,
                {
                    "idle_seconds": round(now - last_activity, 3),
                    "detail": "长时间没有事件：关闭这条连接（任务不受影响），需要继续接收请重连。",
                },
            )
            return
        if now - last_heartbeat >= web.sse_heartbeat_seconds:
            yield HEARTBEAT_FRAME
            last_heartbeat = now
        await asyncio.sleep(POLL_SECONDS)


async def _pump(runtime: ApiRuntime, subscription: EventSubscription) -> AsyncIterator[str]:
    """把订阅的生命周期绑在流上：流结束（含客户端断开）必须退订。"""
    try:
        async for frame in event_stream(runtime, subscription):
            yield frame
    finally:
        runtime.service.unsubscribe_events(subscription)


def build_events_router(runtime: ApiRuntime) -> APIRouter:
    """SSE 路由（与业务路由分开：它写的是流式响应，不是 JSON）。"""
    router = APIRouter(prefix="/api")
    service = runtime.service

    @router.get(
        "/sessions/{session_id}/events",
        summary="会话事件流（SSE）",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "会话事件流：先 `session_snapshot`，再实时增量；`:` 开头是心跳",
                "content": {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}},
            },
            404: {"model": ErrorResponse, "description": "会话不存在"},
        },
    )
    async def session_events(session_id: str) -> StreamingResponse:
        # 未知会话在**建流之前**报错：返回 JSON 404，而不是一条随即结束的事件流
        # （否则客户端只会看到"连上了又断了"，不知道是会话号错了）。
        subscription = service.subscribe_events(session_id)
        return StreamingResponse(
            _pump(runtime, subscription),
            media_type=SSE_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-store",
                # 反向代理（如果将来有）不得缓冲事件流。
                "X-Accel-Buffering": "no",
            },
        )

    return router
