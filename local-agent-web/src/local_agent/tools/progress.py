"""工具的临时展示通知；执行结果仍由图节点持久化。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolProgress:
    run_id: str
    tool_call_id: str
    arguments: dict[str, str] = field(default_factory=dict)
    stream: str = ""
    text: str = ""


_LISTENER: ContextVar[Callable[[ToolProgress], None] | None] = ContextVar(
    "tool_progress_listener", default=None,
)
_CALL: ContextVar[tuple[str, str] | None] = ContextVar("tool_progress_call", default=None)


@contextmanager
def use_tool_listener(listener: Callable[[ToolProgress], None]) -> Iterator[None]:
    token = _LISTENER.set(listener)
    try:
        yield
    finally:
        _LISTENER.reset(token)


def _notify(progress: ToolProgress) -> None:
    listener = _LISTENER.get()
    if listener is not None:
        try:
            listener(progress)
        except Exception:
            logging.getLogger(__name__).exception("工具进度展示通知失败")


@contextmanager
def shell_progress(run_id: str, call_id: str, arguments: dict[str, str]) -> Iterator[None]:
    token = _CALL.set((run_id, call_id))
    try:
        _notify(ToolProgress(run_id, call_id, arguments=arguments))
        yield
    finally:
        _CALL.reset(token)


def current_call_id() -> str | None:
    """当前工具调用的 id（2026-10-21：转后台命令用它当句柄）。

    不在工具调用上下文里时返回 ``None``——调用方据此自己生成一个 id，
    而不是把 ``None`` 当成"没有后台能力"。
    """
    call = _CALL.get()
    return None if call is None else call[1]


def output_reporter() -> Callable[[str, str], None]:
    call = _CALL.get()
    listener = _LISTENER.get()

    def report(stream: str, text: str) -> None:
        if call is not None and listener is not None and text:
            _notify(ToolProgress(*call, stream=stream, text=text))

    return report
