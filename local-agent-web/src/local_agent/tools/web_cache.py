"""搜索结果的内存 TTL 缓存 + single-flight（2026-10-23）。

要解决的是很具体的一件事：同一次对话里模型把同一句查询发两遍（2026-10-23 的实测轨迹里，
第 1 次和第 3 次的查询词几乎相同），第二遍既慢又没有任何新信息。

三条边界：
- **进程内**：服务是单进程单 worker（§15.1），不需要跨进程一致性；
- **只缓存成功结果**：失败/超时下次必须重试，不能被缓存成"永远失败"；
- **同 key 并发只跑一次**：并行工具调用可能同时发同一句查询，其余调用等它跑完直接复用。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 128
#: 等别人跑完的上限：超时后自己重试，避免一个卡死的请求拖住所有同 key 调用。
WAIT_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TtlCache(Generic[T]):
    """线程安全的小缓存；``fetch`` 保证同一 key 同时只有一个 producer 在跑。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, _Entry[T]] = OrderedDict()
        self._inflight: dict[Hashable, threading.Event] = {}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, key: Hashable) -> T | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return entry.value

    def store(self, key: Hashable, value: T) -> None:
        with self._lock:
            self._entries[key] = _Entry(value, self._clock() + self._ttl)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def fetch(self, key: Hashable, producer: Callable[[], T]) -> tuple[T, bool]:
        """返回 ``(值, 是否命中缓存)``；同 key 并发时只有一个调用方真正执行 producer。"""
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None and entry.expires_at > self._clock():
                    self._entries.move_to_end(key)
                    return entry.value, True
                wait = self._inflight.get(key)
                if wait is None:
                    wait = threading.Event()
                    self._inflight[key] = wait
                    break
            # 别人正在跑同一条查询：等它结束再回环查缓存（不重复请求上游）。
            wait.wait(timeout=WAIT_TIMEOUT_SECONDS)

        try:
            value = producer()
        except BaseException:
            with self._lock:
                self._inflight.pop(key, None)
            wait.set()
            raise

        self.store(key, value)
        with self._lock:
            self._inflight.pop(key, None)
        wait.set()
        return value, False
