"""最终回答增量的运行期接收方；与具体页面和传输协议无关。"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Protocol


class AnswerSink(Protocol):
    def push(self, text: str) -> None: ...

    def discard(self) -> None: ...


_SINK: ContextVar[AnswerSink | None] = ContextVar("answer_sink", default=None)


@contextlib.contextmanager
def use_sink(sink: AnswerSink | None) -> Iterator[None]:
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def current_sink() -> AnswerSink | None:
    return _SINK.get()
