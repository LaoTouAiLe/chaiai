"""搜索缓存（2026-10-23）：TTL、失败不缓存、同 key 并发只跑一次。"""

from __future__ import annotations

import threading
import time

import pytest

from local_agent.tools.web_cache import TtlCache


def test_reuses_the_value_inside_the_ttl() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_seconds=10, clock=lambda: now[0])
    calls: list[int] = []

    def produce() -> str:
        calls.append(1)
        return "value"

    first, first_cached = cache.fetch("k", produce)
    second, second_cached = cache.fetch("k", produce)

    assert (first, second) == ("value", "value")
    assert (first_cached, second_cached) == (False, True)
    assert len(calls) == 1


def test_expires_after_the_ttl() -> None:
    now = [0.0]
    cache: TtlCache[str] = TtlCache(ttl_seconds=10, clock=lambda: now[0])
    calls: list[int] = []

    def produce() -> str:
        calls.append(1)
        return f"value-{len(calls)}"

    cache.fetch("k", produce)
    now[0] = 10.5
    value, cached = cache.fetch("k", produce)

    assert cached is False
    assert value == "value-2"
    assert len(calls) == 2


def test_failures_are_never_cached() -> None:
    cache: TtlCache[str] = TtlCache()

    def boom() -> str:
        raise RuntimeError("upstream down")

    with pytest.raises(RuntimeError):
        cache.fetch("k", boom)

    value, cached = cache.fetch("k", lambda: "recovered")
    assert (value, cached) == ("recovered", False)


def test_concurrent_callers_share_one_upstream_call() -> None:
    cache: TtlCache[str] = TtlCache()
    calls: list[float] = []
    outcomes: list[tuple[str, bool]] = []
    barrier = threading.Barrier(2)

    def produce() -> str:
        calls.append(time.monotonic())
        time.sleep(0.15)  # 让第二个线程一定落在 inflight 等待里
        return "shared"

    def worker() -> None:
        barrier.wait()
        outcomes.append(cache.fetch("k", produce))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(calls) == 1
    assert sorted(cached for _value, cached in outcomes) == [False, True]
