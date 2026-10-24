"""可中断的模型调用（§11.7 的传输层流式、§17.4 的第三个动作；2026-10-13）。

生产适配器采用原生 `.astream()`：同步图节点在独立传输循环上等待下一块，取消令牌直接
取消该取块任务，包括等待响应头或首块时的 HTTP 等待。任务退出与响应关闭后才交还控制权；
不是在另一个线程里遗弃仍然执行的请求。SDK 异步客户端在同一循环上复用连接。

仅提供同步 `.stream()` 的自定义适配器无法强杀正在执行的 Python 迭代器，仍按块边界停止；
完全不支持流式或发生普通调用降级时，在调用前、返回后以及异常路径核对令牌。
展示开关不决定能否中断，取消不会触发新的降级请求。

本模块**不做**降级与重试：令牌一旦触发就必须停下来，重试等于把"停止"变成"再跑一次"。
展示与否（`llm.streaming`）与这里无关——那是 `ui.streaming` 的职责。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from local_agent.cancellation import CancellationToken

__all__ = ["TurnCancelled", "invoke_checked", "iter_chunks", "raise_if_cancelled", "stream_supports"]

#: 同步调用降级的限制说明；不得将它描述成立即中断。
UNINTERRUPTIBLE_NOTE = (
    "这条连接不支持流式，本次模型调用只能在它结束后停止（不能立即中断）"
)


class TurnCancelled(Exception):
    """本轮已被取消（§17.4）：调用方据此产出**终态**，而不是当成失败去重试。

    它继承 `Exception` 而不是 `BaseException`：与"模型调用失败"一样要被节点捕获，区别只在于
    节点捕获后**不重试、不记错误事件**，而是收敛成 `cancelled`。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.response: Any = None


_T = TypeVar("_T")


def invoke_checked(call: Callable[[], _T], cancellation: CancellationToken | None) -> _T:
    """普通调用只能在边界停止；已取消不调用，返回/异常后也核对取消。"""
    raise_if_cancelled(cancellation)
    try:
        result = call()
    except Exception:
        raise_if_cancelled(cancellation)
        raise
    if cancellation is not None and cancellation.cancelled:
        stopped = TurnCancelled(cancellation.refusal_reason())
        stopped.response = result
        raise stopped
    return result


def stream_supports(model: Any) -> bool:
    """模型是否提供 `.stream()`（与 `ModelCapabilities.streaming` 同一口径）。"""
    return callable(getattr(model, "stream", None))


def iter_chunks(
    model: Any,
    messages: Sequence[BaseMessage],
    *,
    cancellation: CancellationToken | None,
    supported: bool = True,
) -> Iterator[Any]:
    """逐块产出模型输出；原生异步流等待可取消，同步流按块边界判定（§11.7）。

    Args:
        model: 聊天模型适配器。
        messages: 出站消息。
        cancellation: 本轮取消令牌；``None`` 表示这次调用**不需要**可中断（例如没有运行令牌的
            脚本路径）——注意它**不等于**"不要流式"：展示流式与否由调用方决定，这里照旧逐块产出。
        supported: 能力探测结论（``capabilities.streaming``）。为 ``False`` 时**不假装**能中断：
            退回一次普通调用，由调用方在页面上写明"本轮会在这次调用结束后停止"。

    Yields:
        模型输出的分片（``AIMessageChunk``）。

    Raises:
        TurnCancelled: 令牌已触发。**在抛之前会关闭底层流对象**——那就是"断掉这次响应"，
            与参照实现 drop 流 / `iterator.return()` 同一语义。
    """
    streamer = getattr(model, "stream", None)
    raise_if_cancelled(cancellation)
    if not supported or not callable(streamer):
        response = model.invoke(list(messages))
        raise_if_cancelled(cancellation)
        yield response
        return

    if cancellation is not None and _has_async_transport(model):
        yield from _async_chunks(model, messages, cancellation)
        return
    iterator = streamer(list(messages))
    try:
        while True:
            raise_if_cancelled(cancellation)
            try:
                chunk = next(iterator)
            except StopIteration:
                raise_if_cancelled(cancellation)
                break
            raise_if_cancelled(cancellation)
            yield chunk
    except Exception:
        raise_if_cancelled(cancellation)
        raise
    finally:
        # 关闭生成器 = 关掉这次 HTTP 响应：不关的话连接会挂到对端把内容推完，
        # "停止"就只是本地不再看它，账单照走（与 dsh 的 `iterator.return()` 同一动机）。
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


def raise_if_cancelled(cancellation: CancellationToken | None) -> None:
    if cancellation is not None and cancellation.cancelled:
        raise TurnCancelled(cancellation.refusal_reason())


def _has_async_transport(model: Any) -> bool:
    # RunnableBinding 代理原模型；BaseChatModel 默认 astream 只是把同步迭代放进线程，
    # 取消那个 Future 不会关闭同步连接，不能把它当成真正的异步传输。
    bound = getattr(model, "bound", model)
    if isinstance(bound, BaseChatModel) and type(bound)._astream is BaseChatModel._astream:
        return False
    return callable(getattr(model, "astream", None))


def _async_chunks(model: Any, messages: Sequence[BaseMessage], cancellation: CancellationToken) -> Iterator[Any]:
    """同步图节点驱动原生异步流；取消当前取块任务会关闭 SDK 的 HTTP 响应。"""
    # SDK 的 AsyncClient 会复用连接；每次新建/关闭事件循环会使第二次调用复用到已关闭的循环。
    # 所有原生异步模型传输共用一个进程内循环，具体流和取块任务仍逐调用关闭。
    loop = _transport_loop()
    iterator = model.astream(list(messages))

    async def pull() -> Any:
        pending = asyncio.current_task()
        assert pending is not None
        def stop(_reason: str) -> None:
            loop.call_soon_threadsafe(pending.cancel)
        cancellation.add_cancel_hook(stop)
        try:
            raise_if_cancelled(cancellation)
            return await iterator.__anext__()
        except asyncio.CancelledError:
            raise_if_cancelled(cancellation)
            raise
        finally:
            cancellation.remove_cancel_hook(stop)

    async def close() -> None:
        await iterator.aclose()

    try:
        while True:
            raise_if_cancelled(cancellation)
            try:
                chunk = asyncio.run_coroutine_threadsafe(pull(), loop).result()
            except StopAsyncIteration:
                raise_if_cancelled(cancellation)
                break
            except Exception:
                raise_if_cancelled(cancellation)
                raise
            raise_if_cancelled(cancellation)
            yield chunk
    finally:
        asyncio.run_coroutine_threadsafe(close(), loop).result()


_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def _transport_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="model-transport", daemon=True).start()
        return _loop
