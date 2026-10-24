"""命令级「转后台」（2026-10-21）：不杀进程、能读增量、能结束。"""

from __future__ import annotations

import sys
import threading
import time

from local_agent.security.command_registry import CommandRegistry
from local_agent.security.process_guard import ProcessRegistry

#: 打印一行、再活一会儿：足够让测试先看到输出，再验证"进程还活着"。
SCRIPT = "import time,sys;print('hello-bg',flush=True);time.sleep(3)"


def _start(registry: CommandRegistry, processes: ProcessRegistry, command_id: str = "c1"):
    event = registry.open(command_id)
    assert event is not None
    return processes.run(
        [sys.executable, "-c", SCRIPT],
        timeout_seconds=None,
        detach_event=event,
        on_detached=lambda process, collected: registry.attach(command_id, process, collected),
    )


def test_detach_returns_without_killing_the_process() -> None:
    processes = ProcessRegistry()
    registry = CommandRegistry(processes=processes)
    threading.Thread(target=lambda: (time.sleep(0.4), registry.detach("c1")), daemon=True).start()

    outcome = _start(registry, processes)

    assert outcome.detached is True
    assert outcome.timed_out is False
    assert outcome.exit_code is None
    # 关键语义：进程仍在跑（不是被杀掉后返回 None）。
    assert processes  # 保持引用，避免被回收后看不出问题
    time.sleep(0.2)
    snapshot = registry.read("c1")
    assert snapshot["found"] is True
    assert snapshot["status"] == "running"
    assert "hello-bg" in snapshot["stdout"]


def test_read_advances_with_cursor_and_stop_terminates() -> None:
    processes = ProcessRegistry()
    registry = CommandRegistry(processes=processes)
    threading.Thread(target=lambda: (time.sleep(0.4), registry.detach("c2")), daemon=True).start()
    _start(registry, processes, "c2")
    time.sleep(0.2)

    first = registry.read("c2")
    cursor = first["next_cursor"]
    second = registry.read("c2", cursor=cursor)
    # 游标推进后不重复返回同一段。
    assert second["stdout"] == ""

    assert registry.stop("c2") is True
    time.sleep(0.5)
    final = registry.read("c2")
    assert final["status"] == "finished"
    assert final["note"] == "模型主动结束后台命令"


def test_detach_rejects_unknown_command() -> None:
    processes = ProcessRegistry()
    registry = CommandRegistry(processes=processes)
    assert registry.detach("nope") is False
    assert registry.read("nope")["found"] is False
    assert registry.stop("nope") is False
