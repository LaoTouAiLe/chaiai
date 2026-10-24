"""只并行工具调用，不在线程中合并 Graph 状态或操作 Checkpoint。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, TypeVar

from langchain_core.messages.tool import ToolCall

from local_agent.config import WebAccessConfig

T = TypeVar("T")

# 采用明确的只读工具契约；不能把“属于只读能力”泛化为可并行，
# 例如 delegate 会启动子任务，read_shell 会推进日志游标。
PARALLEL_READ_TOOLS = frozenset({
    "list_dir", "read_file", "grep", "glob", "web_search", "web_fetch",
    "git_status", "git_diff", "git_log", "skill_list", "skill_load",
    "view_image", "inspect_system",
})


def parallel_read_allowed(call: ToolCall, config: WebAccessConfig) -> bool:
    """判断工具契约能否并行；落盘与计数额度仍走原串行路径。"""
    name = call["name"]
    if name not in PARALLEL_READ_TOOLS:
        return False
    if name in {"grep", "glob"} and (call.get("args") or {}).get("save_full"):
        return False
    if name == "web_search" and config.max_search_calls is not None:
        return False
    return not (
        name == "web_fetch"
        and (config.max_read_pages is not None or config.max_reads_per_page is not None)
    )


@dataclass(frozen=True)
class TimedToolResult(Generic[T]):
    """真实工作线程起止时间与工具返回值。"""

    value: T
    started_at: datetime
    finished_at: datetime


def invoke_read_batch(
    calls: Sequence[tuple[int, ToolCall]], invoke: Callable[[ToolCall], T],
) -> dict[int, TimedToolResult[T]]:
    """并行连续只读调用，以原始位置返回结果，给每个线程单独复制上下文。"""
    def timed(call: ToolCall) -> TimedToolResult[T]:
        started = datetime.now(timezone.utc)
        value = invoke(call)
        return TimedToolResult(value, started, datetime.now(timezone.utc))

    # 采用标准库有界工作线程数；队列不限制任务总数，也不新增单任务执行次数上限。
    with ThreadPoolExecutor(thread_name_prefix="tool-read") as executor:
        futures = [(index, executor.submit(copy_context().run, timed, call))
                   for index, call in calls]
        return {index: future.result() for index, future in futures}
