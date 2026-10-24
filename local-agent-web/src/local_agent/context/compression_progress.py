"""压缩的运行期展示通知；完成事实仍由节点写入 Checkpoint。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal, TypedDict


class CompressionProgress(TypedDict):
    operation_id: str
    run_id: str
    state: Literal["running", "completed", "failed"]
    used_tokens: int
    window_tokens: int
    completion_reserve_tokens: int


_LISTENER: ContextVar[Callable[[CompressionProgress], None] | None] = ContextVar(
    "compression_listener", default=None,
)


@contextmanager
def use_compression_listener(listener: Callable[[CompressionProgress], None]) -> Iterator[None]:
    token = _LISTENER.set(listener)
    try:
        yield
    finally:
        _LISTENER.reset(token)


def notify_compression(progress: CompressionProgress) -> None:
    listener = _LISTENER.get()
    if listener is not None:
        try:
            listener(progress)
        except Exception:
            logging.getLogger(__name__).exception("压缩进度展示通知失败")
