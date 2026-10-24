"""多 server 编排：连接池、失败隔离、容量淘汰、空闲回收（实施文档 M2）。

这里测的是**规模行为**，不是"能不能调通"（那在 ``test_mcp_client.py``）：

- 一台崩了不拖累其他；
- 失败进冷却期，不会每轮都重新 spawn；
- 超过容量淘汰最久未用；
- 空闲断开后回到 ``idle`` 而不是 ``disabled``（目录还在，重连是懒连接）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from local_agent.config import McpConfig
from local_agent.mcp.config import McpServersFile
from local_agent.mcp.errors import KIND_NOT_CONNECTED, McpFailure
from local_agent.mcp.manager import (
    STATUS_CONNECTED,
    STATUS_DISABLED,
    STATUS_FAILED,
    STATUS_IDLE,
    McpManager,
    ServerState,
)

FAKE_SERVER = Path(__file__).with_name("mcp_fake_server.py")


def _spec(env: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_SERVER)],
        "env": env or {},
    }


def _servers(**specs: dict[str, object]) -> McpServersFile:
    return McpServersFile.model_validate({"mcpServers": specs})


def _manager(
    servers: McpServersFile,
    states: dict[str, ServerState],
    *,
    max_connected_servers: int = 4,
    max_direct_tools: int = 20,
    idle_timeout_seconds: int = 600,
    sweep_interval: float = 60.0,
    failure_cooldown: float = 60.0,
) -> McpManager:
    config = McpConfig(
        enabled=True,
        max_connected_servers=max_connected_servers,
        max_direct_tools=max_direct_tools,
        idle_timeout_seconds=idle_timeout_seconds,
        startup_timeout_seconds=20,
        call_timeout_seconds=10,
        max_tool_schema_bytes=8_192,
    )
    return McpManager(
        servers,
        config=config,
        state_provider=lambda: states,
        sweep_interval=sweep_interval,
        failure_cooldown=failure_cooldown,
    )


def test_disabled_server_is_never_spawned() -> None:
    manager = _manager(_servers(a=_spec()), {"a": ServerState(enabled=False)})
    try:
        manager.sync()
        snapshot = manager.snapshot()

        assert snapshot.servers[0].status == STATUS_DISABLED
        assert snapshot.servers[0].tool_count == 0
        assert snapshot.direct == ()
        assert manager.spawn_attempts("a") == 0
        with pytest.raises(McpFailure) as excinfo:
            manager.call_tool("a", "echo", {"text": "x"})
        assert excinfo.value.kind == KIND_NOT_CONNECTED
    finally:
        manager.close()


def test_enabled_server_connects_and_is_callable() -> None:
    manager = _manager(_servers(a=_spec()), {"a": ServerState(enabled=True)})
    try:
        manager.sync()
        snapshot = manager.snapshot()

        assert snapshot.servers[0].status == STATUS_CONNECTED
        assert snapshot.servers[0].tool_count == 3
        assert snapshot.direct == (
            "mcp__a__boom",
            "mcp__a__echo",
            "mcp__a__sleep_for",
        )

        outcome = manager.call_tool("a", "echo", {"text": "你好"})
        assert outcome.text == "你好"
    finally:
        manager.close()


def test_direct_quota_pushes_the_rest_into_search() -> None:
    manager = _manager(
        _servers(a=_spec()), {"a": ServerState(enabled=True)}, max_direct_tools=1
    )
    try:
        manager.sync()
        snapshot = manager.snapshot()

        assert len(snapshot.direct) == 1
        assert len(snapshot.searchable) == 2
        assert {entry.server for entry in snapshot.searchable} == {"a"}
    finally:
        manager.close()


def test_one_crashing_server_does_not_block_the_others() -> None:
    servers = _servers(good=_spec(), bad=_spec({"FAKE_MCP_MODE": "crash"}))
    states = {"good": ServerState(enabled=True), "bad": ServerState(enabled=True)}
    manager = _manager(servers, states)
    try:
        manager.sync()
        views = {view.name: view for view in manager.snapshot().servers}

        assert views["good"].status == STATUS_CONNECTED
        assert views["bad"].status == STATUS_FAILED
        assert views["bad"].failure_kind is not None
        assert views["bad"].failure_message

        # 关键：好的一台照常可调。
        assert manager.call_tool("good", "echo", {"text": "ok"}).text == "ok"
    finally:
        manager.close()


def test_failed_server_is_not_respawned_within_cooldown() -> None:
    servers = _servers(bad=_spec({"FAKE_MCP_MODE": "crash"}))
    manager = _manager(servers, {"bad": ServerState(enabled=True)}, failure_cooldown=60.0)
    try:
        manager.sync()
        attempts = manager.spawn_attempts("bad")
        assert attempts == 1

        with pytest.raises(McpFailure):
            manager.call_tool("bad", "echo", {})
        with pytest.raises(McpFailure):
            manager.call_tool("bad", "echo", {})

        # 冷却期内不再 spawn：否则每轮对话都要等一次必然失败的启动。
        assert manager.spawn_attempts("bad") == attempts
    finally:
        manager.close()


def test_capacity_evicts_the_least_recently_used() -> None:
    servers = _servers(a=_spec(), b=_spec())
    states = {"a": ServerState(enabled=True), "b": ServerState(enabled=True)}
    manager = _manager(servers, states, max_connected_servers=1)
    try:
        manager.sync()
        views = {view.name: view for view in manager.snapshot().servers}

        # 按配置顺序连：a 先连上，连 b 时容量已满，淘汰最久未用的 a。
        assert views["a"].status == STATUS_IDLE
        assert views["b"].status == STATUS_CONNECTED
    finally:
        manager.close()


def test_idle_reaper_disconnects_but_keeps_the_catalog() -> None:
    manager = _manager(
        _servers(a=_spec()),
        {"a": ServerState(enabled=True)},
        idle_timeout_seconds=1,
        sweep_interval=0.2,
    )
    try:
        manager.start()
        manager.sync()
        assert manager.snapshot().servers[0].status == STATUS_CONNECTED

        time.sleep(1.8)

        view = manager.snapshot().servers[0]
        # 断开 ≠ 关闭：开关还开着，只是连接回收了，目录也还在。
        assert view.status == STATUS_IDLE
        assert view.enabled is True
        assert view.tool_count == 3
    finally:
        manager.close()


def test_concurrent_first_calls_share_one_connection() -> None:
    from concurrent.futures import ThreadPoolExecutor
    manager = _manager(_servers(a=_spec()), {"a": ServerState(enabled=True)})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(manager.call_tool, "a", "echo", {"text": str(i)}) for i in range(2)]
            assert [future.result().text for future in futures] == ["0", "1"]
        assert manager.spawn_attempts("a") == 1
    finally:
        manager.close()


def test_changed_spec_replaces_connection_and_clears_disconnected_catalog() -> None:
    states = {"a": ServerState(enabled=True)}
    manager = _manager(_servers(a=_spec({"FAKE_MCP_PREFIX": "old:"})), states)
    try:
        assert manager.call_tool("a", "echo", {"text": "x"}).text == "old:x"
        manager.replace_servers(_servers(a=_spec({"FAKE_MCP_PREFIX": "new:"})))
        assert manager.snapshot().servers[0].tool_count == 0
        assert manager.call_tool("a", "echo", {"text": "x"}).text == "new:x"
        states["a"] = ServerState(enabled=False)
        manager.sync()
        manager.replace_servers(_servers())
        assert "a" not in manager._catalogs
    finally:
        manager.close()


def test_catalog_cache_restores_without_starting_a_process(tmp_path) -> None:
    path = tmp_path / "catalog.sqlite"
    servers = _servers(a=_spec({"TOKEN": "cache-must-not-store-this-secret"}))
    states = {"a": ServerState(enabled=True)}
    config = McpConfig(startup_timeout_seconds=20, call_timeout_seconds=10)
    first = McpManager(servers, config=config, state_provider=lambda: states, cache_path=path)
    try:
        first.sync()
        assert first.snapshot().servers[0].tool_count == 3
    finally:
        first.close()
    second = McpManager(servers, config=config, state_provider=lambda: states, cache_path=path)
    try:
        second.start()
        time.sleep(0.1)
        assert second.snapshot().servers[0].tool_count == 3
        assert second.spawn_attempts("a") == 0
        assert second.call_tool("a", "echo", {"text": "cached"}).text == "cached"
        assert second.spawn_attempts("a") == 1
    finally:
        second.close()
    assert b"cache-must-not-store-this-secret" not in path.read_bytes()
    changed = McpManager(_servers(a=_spec({"TOKEN": "rotated"})), config=config,
                         state_provider=lambda: states, cache_path=path)
    assert changed.snapshot().servers[0].tool_count == 0
    changed.close()


def test_busy_connection_is_not_reaped_or_evicted() -> None:
    from concurrent.futures import ThreadPoolExecutor
    manager = _manager(_servers(a=_spec(), b=_spec()), {
        "a": ServerState(enabled=True), "b": ServerState(enabled=True),
    }, max_connected_servers=1, idle_timeout_seconds=1, sweep_interval=0.1)
    try:
        manager.call_tool("a", "echo", {"text": "ready"})
        # Start only the reaper: prewarming b would intentionally change the pool.
        import threading
        manager._reaper_thread = threading.Thread(target=manager._reaper_loop, daemon=True)
        manager._reaper_thread.start()
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(manager.call_tool, "a", "sleep_for", {"seconds": 2})
            time.sleep(1.3)
            assert manager.snapshot().servers[0].status == STATUS_CONNECTED
            with pytest.raises(McpFailure):
                manager.call_tool("b", "echo", {"text": "busy"})
            assert pending.result().text == "slept 2.0"
    finally:
        manager.close()


def test_changed_lists_block_a_previously_bound_tool() -> None:
    states = {"a": ServerState(enabled=True)}
    manager = _manager(_servers(a=_spec()), states)
    try:
        manager.sync()
        states["a"] = ServerState(enabled=True, disabled_tools=("echo",))
        with pytest.raises(McpFailure):
            manager.call_tool("a", "echo", {"text": "must not run"})
    finally:
        manager.close()


def test_server_dying_during_a_call_is_closed_and_cooled_down() -> None:
    manager = _manager(_servers(a=_spec({"FAKE_MCP_CRASH_TEXT": "crash-now"})),
                       {"a": ServerState(enabled=True)})
    try:
        with pytest.raises(McpFailure) as failure:
            manager.call_tool("a", "echo", {"text": "crash-now"})
        assert failure.value.kind == "server_exited"
        assert manager.snapshot().servers[0].status == STATUS_FAILED
        with pytest.raises(McpFailure):
            manager.call_tool("a", "echo", {"text": "not-retried"})
        assert manager.spawn_attempts("a") == 1
    finally:
        manager.close()


def test_explicit_reload_refreshes_a_live_catalog() -> None:
    manager = _manager(_servers(a=_spec()), {"a": ServerState(enabled=True)})
    try:
        manager.sync()
        before = manager._slots["a"].worker
        manager.replace_servers(_servers(a=_spec()), refresh=True)
        manager.sync()
        assert not before.alive
        assert manager._slots["a"].worker is not before
        assert manager.snapshot().servers[0].tool_count == 3
    finally:
        manager.close()
