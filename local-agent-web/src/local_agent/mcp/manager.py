"""多台 MCP Server 的编排：连接池、预热、空闲回收、失败隔离（架构 §6.13；M2）。

三条规模约束决定了这里的形状（实施文档 §3）：

- **配置 N 个 ≠ 连 N 个**：只有启用的才连，且连接在**后台预热线程**里串行进行——
  一次性拉起 50 个 ``npx`` 会把网络与磁盘打满，串行让每个 server 的失败原因都写得清楚。
- **连 N 个 ≠ 常驻 N 个**：超过 ``max_connected_servers`` 时淘汰**最久未用**的连接；
  空闲超过 ``idle_timeout_seconds`` 同样断开。工具目录留在内存里，重连是懒连接。
- **失败隔离**：一台 server 起不来只影响它自己；失败进冷却期（``failure_cooldown_seconds``），
  冷却内不再 spawn——否则每轮对话都会重试一次，把"这台起不来"变成"每次都很卡"。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_agent.config import McpConfig
from local_agent.mcp.cache import CatalogCache
from local_agent.mcp.catalog import (
    ExposureRow,
    ServerCatalog,
    ToolEntry,
    ToolExposure,
    build_server_catalog,
    deferred_entries,
    direct_names,
    expose_all,
)
from local_agent.mcp.client import CallOutcome, ServerWorker
from local_agent.mcp.config import McpServersFile
from local_agent.mcp.errors import KIND_NOT_CONNECTED, KIND_SERVER_EXITED, McpFailure
from local_agent.mcp.state import DEFAULT_SERVER_STATE, ServerState

#: 连接状态闭集（DTO 直接复用，页面不会遇到没见过的状态）。
STATUS_DISABLED = "disabled"
STATUS_IDLE = "idle"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_FAILED = "failed"

#: 失败冷却：同一台 server 在冷却期内不再尝试 spawn。
DEFAULT_FAILURE_COOLDOWN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ServerView:
    """一台 server 的对外快照（页面用，不含任何凭据）。"""

    name: str
    enabled: bool
    status: str
    tool_count: int
    failure_kind: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True, slots=True)
class McpSnapshot:
    """某一刻的全部 MCP 事实：每台 server 的状态 + 分档结果。

    拿到它的人**不需要**知道连接细节；工具绑定只读 ``direct`` / ``searchable``。
    """

    servers: tuple[ServerView, ...]
    rows: tuple[ExposureRow, ...] = ()
    direct: tuple[str, ...] = ()
    searchable: tuple[ToolEntry, ...] = ()

    def entries(self) -> tuple[ToolEntry, ...]:
        """全部已知工具（含 ``Hidden``，只用于界面展示）。"""
        return tuple(row.entry for row in self.rows)


@dataclass(slots=True)
class _Slot:
    """一条活连接。**不持有目录**：目录比连接活得久（断开后界面仍要显示工具清单）。"""

    worker: ServerWorker
    last_used: float
    active: int = 0


class McpManager:
    """连接池 + 后台预热。线程安全（一把锁保护池，调用路径不持锁执行工具）。"""

    def __init__(
        self,
        servers: McpServersFile,
        *,
        config: McpConfig,
        state_provider: Callable[[], Mapping[str, ServerState]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sweep_interval: float = 30.0,
        failure_cooldown: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
        cache_path: Path | None = None,
    ) -> None:
        self._servers = servers
        self._config = config
        self._state_provider = state_provider or (dict)
        self._clock = clock
        self._sweep_interval = max(0.05, sweep_interval)
        self._failure_cooldown = failure_cooldown

        self._lock = threading.RLock()
        self._connection_lock = threading.RLock()
        self._cache = None if cache_path is None else CatalogCache(cache_path)
        self._starting: dict[str, ServerWorker] = {}
        self._slots: dict[str, _Slot] = {}
        self._failures: dict[str, tuple[McpFailure, float]] = {}
        self._connecting: set[str] = set()
        #: 每台 server 实际尝试启动过几次。**不是给测试看的**：诊断时"一直失败"和
        #: "每轮都在重试"是两种完全不同的故障，没有这个计数就只能靠猜。
        self._spawn_attempts: dict[str, int] = {}
        #: 已知的工具目录，**与连接解耦**：空闲回收或暂时失败后，界面仍能显示上次看到的工具。
        #: 只有这台 server 的 ``tools/list`` 变了才更新（重连成功时）。
        self._catalogs: dict[str, tuple[ToolEntry, ...]] = {}

        if self._cache is not None:
            for name, spec in servers.mcp_servers.items():
                cached = self._cache.load(name, spec)
                if cached is not None:
                    self._catalogs[name] = cached.tools

        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._refresh = threading.Event()
        self._prewarm_thread: threading.Thread | None = None
        self._reaper_thread: threading.Thread | None = None

    # --- 生命周期 ------------------------------------------------------------

    def start(self) -> None:
        """起后台线程并请求一次预热。**不阻塞**：预热失败不影响对话启动。"""
        if self._prewarm_thread is not None:
            return
        self._prewarm_thread = threading.Thread(
            target=self._prewarm_loop, name="mcp-prewarm", daemon=True
        )
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, name="mcp-reaper", daemon=True
        )
        self._prewarm_thread.start()
        self._reaper_thread.start()
        self._wake.set()

    def close(self, *, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        with self._lock:
            starting = list(self._starting.values())
            slots = list(self._slots.values())
            self._slots.clear()
        for worker in starting:
            worker.close(timeout=timeout)
        for slot in slots:
            slot.worker.close(timeout=timeout)
        for thread in (self._prewarm_thread, self._reaper_thread):
            if thread is not None:
                thread.join(timeout=timeout)
        self._prewarm_thread = None
        self._reaper_thread = None

    def warm(self) -> None:
        """请求一次后台同步（新增 server、改开关后调用）。**合并**连续请求。"""
        self._refresh.set()
        self._wake.set()

    def replace_servers(self, servers: McpServersFile, *, refresh: bool = False) -> None:
        """换一份 ``mcp.json`` 配置（"重载"/"添加 server"走这里）。

        先断掉新配置里**已经不存在**的 server（连接与目录一起清），再请求预热。不清目录的话，
        界面会一直显示一台已经被用户删掉的 server 的工具。
        """
        with self._connection_lock:
            with self._lock:
                changed = {name for name, spec in self._servers.mcp_servers.items()
                           if refresh or servers.mcp_servers.get(name) != spec}
                removed_slots = [self._slots.pop(name) for name in changed if name in self._slots]
                for name in changed:
                    self._catalogs.pop(name, None)
                    self._failures.pop(name, None)
                    self._spawn_attempts.pop(name, None)
                    if self._cache is not None:
                        self._cache.forget(name)
                self._servers = servers
                self._failures.clear()
            for slot in removed_slots:
                slot.worker.close()
        self.warm()

    # --- 读取 ----------------------------------------------------------------

    def states(self) -> Mapping[str, ServerState]:
        return self._state_provider()

    def spawn_attempts(self, name: str) -> int:
        """这台 server 被实际尝试启动过几次（诊断用）。"""
        with self._lock:
            return self._spawn_attempts.get(name, 0)

    def sync(self) -> None:
        """**立即**同步一次连接池（阻塞到所有应连的 server 处理完）。

        界面上的"重新连接"、改开关后的立即生效都走它；后台预热线程走同一个实现，
        所以两条路径不会出现行为差异。
        """
        self._sync_once()

    def snapshot(self) -> McpSnapshot:
        """当前事实。**不连接、不阻塞**：未连上的 server 报 ``idle``/``failed``。"""
        states = self.states()
        views: list[ServerView] = []
        catalogs: list[ServerCatalog] = []
        enabled_map: dict[str, bool] = {}
        allow_map: dict[str, Sequence[str] | None] = {}
        deny_map: dict[str, Sequence[str]] = {}
        with self._lock:
            slots = dict(self._slots)
            failures = dict(self._failures)
            connecting = set(self._connecting)
            catalogs_by_server = dict(self._catalogs)
            specs = self._servers

        for name, spec in specs.mcp_servers.items():
            state = states.get(name, ServerState(
                enabled_tools=spec.enabled_tools, disabled_tools=spec.disabled_tools))
            slot = slots.get(name)
            recorded = failures.get(name)
            failure = recorded[0] if recorded is not None else None
            enabled_map[name] = state.enabled
            allow_map[name] = state.enabled_tools
            deny_map[name] = state.disabled_tools

            if not state.enabled:
                status = STATUS_DISABLED
            elif slot is not None and slot.worker.alive:
                status = STATUS_CONNECTED
            elif name in connecting:
                status = STATUS_CONNECTING
            elif failure is not None:
                status = STATUS_FAILED
            else:
                status = STATUS_IDLE

            # 目录与连接解耦：未连上但**见过**的 server 也参与分档——否则"重连期间工具突然消失"，
            # 而模型可能正在按上一轮的目录调用它们（工具对象在，调用时懒连接）。
            entries = catalogs_by_server.get(name, ())
            if entries:
                catalogs.append(ServerCatalog(server=name, tools=entries))
            views.append(
                ServerView(
                    name=name,
                    enabled=state.enabled,
                    status=status,
                    tool_count=len(entries),
                    failure_kind=failure.kind if failure is not None else None,
                    failure_message=failure.message if failure is not None else None,
                )
            )

        rows = expose_all(
            catalogs,
            enabled=enabled_map,
            enabled_tools=allow_map,
            disabled_tools=deny_map,
            max_direct=self._config.max_direct_tools,
            max_schema_bytes=self._config.max_tool_schema_bytes,
        )
        return McpSnapshot(
            servers=tuple(views),
            rows=rows,
            direct=direct_names(rows),
            searchable=deferred_entries(rows),
        )

    # --- 调用 ----------------------------------------------------------------

    def call_tool(self, server: str, tool: str, arguments: Mapping[str, Any]) -> CallOutcome:
        """调用某个 server 的一个工具。

        Raises:
            McpFailure: 未启用、未连接、超时、崩溃。**不自动重试**。
        """
        with self._connection_lock:
            state = self.states().get(server, DEFAULT_SERVER_STATE)
            if not state.enabled or self._stopping.is_set():
                raise McpFailure(KIND_NOT_CONNECTED, f"{server} 没有启用，无法调用它的工具。")
            if tool in state.disabled_tools or (
                state.enabled_tools is not None and tool not in state.enabled_tools
            ):
                raise McpFailure(KIND_NOT_CONNECTED, f"{server} 的 {tool} 不在允许调用的名单中。")
            slot = self._ensure_slot(server)
            with self._lock:
                visible = self.snapshot().rows
                if not any(row.entry.server == server and row.entry.name == tool
                           and row.exposure != ToolExposure.HIDDEN for row in visible):
                    raise McpFailure(KIND_NOT_CONNECTED, f"{server} 已不再提供 {tool}。")
                slot.active += 1
                slot.last_used = self._clock()
        try:
            return slot.worker.call_tool(tool, arguments)
        except McpFailure as failure:
            if failure.kind == KIND_SERVER_EXITED:
                with self._lock:
                    if self._slots.get(server) is slot:
                        self._slots.pop(server)
                self._fail(server, failure)
                slot.worker.close()
            raise
        finally:
            with self._lock:
                slot.active -= 1
                slot.last_used = self._clock()

    # --- 内部 ----------------------------------------------------------------

    def _fail(self, name: str, failure: McpFailure) -> None:
        with self._lock:
            self._failures[name] = (failure, self._clock())

    def _recent_failure(self, name: str) -> McpFailure | None:
        with self._lock:
            recorded = self._failures.get(name)
        if recorded is None:
            return None
        failure, at = recorded
        if self._clock() - at >= self._failure_cooldown:
            return None
        return failure

    def _ensure_slot(self, name: str) -> _Slot:
        """取一台**已连上**的 slot；必要时同步建立连接。"""
        with self._lock:
            slot = self._slots.get(name)
            if slot is not None and slot.worker.alive:
                return slot
            if slot is not None:
                # 进程已死：按失败处理，别把它当可用连接。
                self._slots.pop(name, None)
                slot.worker.close(timeout=1.0)
            failure = self._recent_failure(name)
            if failure is not None:
                raise failure
        return self._connect(name)

    def _connect(self, name: str) -> _Slot:
        """建立连接（调用方已确认需要连接）。失败进冷却期。"""
        with self._connection_lock:
            return self._connect_serial(name)

    def _connect_serial(self, name: str) -> _Slot:
        if self._stopping.is_set() or not self.states().get(name, DEFAULT_SERVER_STATE).enabled:
            raise McpFailure(KIND_NOT_CONNECTED, f"{name} 已关闭。")
        with self._lock:
            current = self._slots.get(name)
            if current is not None and current.worker.alive:
                return current
            failure = self._recent_failure(name)
            if failure is not None:
                raise failure
            if len(self._slots) >= self._config.max_connected_servers:
                candidates = [(slot.last_used, key) for key, slot in self._slots.items()
                              if slot.active == 0]
                if not candidates:
                    raise McpFailure(KIND_NOT_CONNECTED, "MCP 连接均在使用中，请稍后再试。")
                _, key = min(candidates)
                victim = self._slots.pop(key)
            else:
                victim = None
        if victim is not None:
            victim.worker.close()
        spec = self._servers.mcp_servers.get(name)
        if spec is None:
            raise McpFailure(KIND_NOT_CONNECTED, f"mcp.json 里没有名为 {name} 的 server。")

        with self._lock:
            self._connecting.add(name)
            self._spawn_attempts[name] = self._spawn_attempts.get(name, 0) + 1
        worker = ServerWorker(
            name,
            spec,
            startup_timeout=self._config.startup_timeout_seconds,
            call_timeout=self._config.call_timeout_seconds,
        )
        with self._lock:
            self._starting[name] = worker
        try:
            raw_tools = worker.start()
        except McpFailure as known:
            worker.close(timeout=1.0)
            self._fail(name, known)
            raise
        except BaseException as exc:
            worker.close(timeout=1.0)
            generic = McpFailure(KIND_NOT_CONNECTED, f"{name} 连接失败：{type(exc).__name__}: {exc}")
            self._fail(name, generic)
            raise generic from exc
        finally:
            with self._lock:
                self._connecting.discard(name)
                self._starting.pop(name, None)

        if self._stopping.is_set() or not self.states().get(name, DEFAULT_SERVER_STATE).enabled:
            worker.close()
            raise McpFailure(KIND_NOT_CONNECTED, f"{name} 已关闭。")
        catalog = build_server_catalog(name, list(raw_tools))
        if self._cache is not None:
            self._cache.save(catalog, spec)
        slot = _Slot(worker=worker, last_used=self._clock())
        with self._lock:
            stopped = self._stopping.is_set()
            if not stopped:
                self._slots[name] = slot
                self._catalogs[name] = catalog.tools
                self._failures.pop(name, None)
        if stopped:
            worker.close()
            raise McpFailure(KIND_NOT_CONNECTED, f"{name} 已关闭。")
        self._evict_if_needed(keep=name)
        return slot

    def _evict_if_needed(self, *, keep: str) -> None:
        """超过容量时淘汰最久未用的连接（``keep`` 除外）。

        **在锁外关进程**：``close`` 要 join 线程，持锁做会阻塞所有调用方。
        """
        capacity = self._config.max_connected_servers
        victims: list[_Slot] = []
        with self._lock:
            while len(self._slots) > capacity:
                candidates = [
                    (slot.last_used, name)
                    for name, slot in self._slots.items()
                    if name != keep and slot.active == 0
                ]
                if not candidates:
                    break
                _, victim_name = min(candidates)
                victims.append(self._slots.pop(victim_name))
        for victim in victims:
            victim.worker.close(timeout=2.0)

    def _sync_once(self, *, cached_lazy: bool = False) -> None:
        """让连接池与当前开关一致：连该连的，断不该连的。"""
        states = self.states()
        with self._lock:
            stale = [
                name
                for name in self._slots
                if not states.get(name, DEFAULT_SERVER_STATE).enabled
            ]
            stale_slots = [self._slots.pop(name) for name in stale]
        for slot in stale_slots:
            slot.worker.close(timeout=2.0)

        with self._lock:
            names = self._servers.names()
        for name in names:
            if self._stopping.is_set():
                return
            if not states.get(name, DEFAULT_SERVER_STATE).enabled:
                continue
            with self._lock:
                current = self._slots.get(name)
                connecting = name in self._connecting
                if (current is not None and current.worker.alive) or connecting:
                    continue
            if self._recent_failure(name) is not None:
                continue
            if cached_lazy and name in self._catalogs:
                continue
            try:
                self._connect(name)
            except McpFailure:
                # 失败已记录在 ``_failures`` 并进了冷却期；预热线程继续下一台。
                continue

    def _prewarm_loop(self) -> None:
        """**请求驱动**的预热，不是定时轮询。

        这一点很关键：如果这里带超时轮询，空闲回收断开的连接会在下一个 tick 被重新拉起，
        形成"回收—重连"死循环，白白烧 CPU 与内存。回收之后就该保持断开，
        直到下一次真实调用（懒连接）或用户点"重新连接"（``warm()``）。
        """
        while not self._stopping.is_set():
            self._wake.wait()
            if self._stopping.is_set():
                return
            self._wake.clear()
            refresh = self._refresh.is_set()
            self._refresh.clear()
            self._sync_once(cached_lazy=not refresh)

    def _reaper_loop(self) -> None:
        """空闲回收：长时间没被调用的连接断开（工具目录保留）。"""
        idle = self._config.idle_timeout_seconds
        while not self._stopping.wait(timeout=self._sweep_interval):
            if idle <= 0:
                continue
            now = self._clock()
            with self._connection_lock, self._lock:
                expired = [
                    name
                    for name, slot in self._slots.items()
                    if slot.active == 0 and now - slot.last_used >= idle
                ]
                expired_slots = [self._slots.pop(name) for name in expired]
            for slot in expired_slots:
                slot.worker.close(timeout=2.0)
