"""单台 MCP Server 的连接（架构 §6.13；实施文档 M2）。

**一 server = 一个线程 = 一个 asyncio 事件循环。** 这不是随手选的形状，是两条约束推出来的：

1. MCP 传输是 async 的，而本项目的工具执行节点是同步函数（``tool_execution_node``）。
   同步侧要能调 async，只能有一个常驻 loop 让它 ``run_coroutine_threadsafe``/投递请求。
2. ``mcp`` SDK 的 ``stdio_client``/``ClientSession`` 建在 anyio 的 task group 上，
   **所有 session 操作必须发生在创建它的那个 task 里**——换 task 会撞
   "cancel scope from a different task"。所以不能让同步侧"随便起个协程"去调 session，
   必须把请求投进**连接 task 自己的队列**，由它串行执行。

由此得到本模块的形状：一个连接 task 跑 ``_serve()``，从 ``asyncio.Queue`` 取请求；
同步侧用 ``concurrent.futures.Future`` 等结果。串行执行也顺带解决了"同一连接并发请求"
的时序问题——单用户本地 Agent 不需要那点并发。

**不做重试**：超时与崩溃都按失败如实上报（``McpFailure`` 带 ``kind``），由编排层决定是否
下次懒连接。静默重试会把"这台 server 一直起不来"藏起来。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from local_agent.mcp.config import McpServerSpec
from local_agent.mcp.errors import (
    KIND_CALL_TIMEOUT,
    KIND_COMMAND_NOT_FOUND,
    KIND_HANDSHAKE_FAILED,
    KIND_NOT_CONNECTED,
    KIND_PROTOCOL_ERROR,
    KIND_SERVER_EXITED,
    KIND_STARTUP_TIMEOUT,
    McpFailure,
)

#: ``tools/list`` 的分页上限。协议允许 server 一直返回 ``nextCursor``；没有上限时
#: 一个坏 server 就能把连接线程钉死在循环里。
MAX_TOOL_PAGES = 50
MAX_RESULT_CHARS = 60_000

#: 同步侧等待的兜底秒数（真正的超时由 loop 内的 ``wait_for`` 控制，这里只防"线程死了没人叫醒"）。
_WAIT_SLACK_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """一次 ``tools/call`` 的文本化结果。

    图片与嵌入资源**不展开**：本版没有把 MCP 返回的二进制接入多模态链路的通道，
    展开成占位说明比伪造一段"图片内容"诚实。
    """

    text: str
    is_error: bool
    content_kinds: tuple[str, ...]


@dataclass(slots=True)
class _Request:
    tool: str
    arguments: Mapping[str, Any]
    future: concurrent.futures.Future[CallOutcome]


def _root_cause(exc: BaseException) -> BaseException:
    """剥掉 anyio/asyncio 的 ExceptionGroup 外壳，取最内层异常。

    不按类型名判断（``BaseExceptionGroup`` 只在 3.11+ 是内建）：任何带 ``exceptions`` 的异常
    都当分组处理，这样 3.10 上也不会漏。
    """
    current = exc
    while True:
        inner = getattr(current, "exceptions", None)
        if not inner:
            return current
        current = inner[0]


def _describe_failure(exc: BaseException, *, name: str, ready: bool) -> McpFailure:
    """把内层异常翻成带 ``kind`` 的失败（消息可直接给用户看）。"""
    root = _root_cause(exc)
    if isinstance(root, McpFailure):
        return root
    if isinstance(root, (FileNotFoundError, PermissionError)):
        return McpFailure(
            KIND_COMMAND_NOT_FOUND,
            f"启动 {name} 失败：找不到命令（{root}）。装好对应的运行时（多数 MCP server 需要 Node.js）后重试。",
        )
    if isinstance(root, asyncio.TimeoutError):
        return McpFailure(
            KIND_STARTUP_TIMEOUT, f"{name} 握手超时：进程起来了但没在限时内完成初始化。"
        )
    if ready and isinstance(root, MCPError):
        kind = (KIND_SERVER_EXITED if root.code == types.CONNECTION_CLOSED else
                KIND_CALL_TIMEOUT if root.code == types.REQUEST_TIMEOUT else KIND_PROTOCOL_ERROR)
        return McpFailure(kind, f"{name} 请求失败：{root.message}")
    if not ready:
        return McpFailure(KIND_HANDSHAKE_FAILED, f"{name} 初始化失败：{type(root).__name__}: {root}")
    return McpFailure(KIND_SERVER_EXITED, f"{name} 已退出：{type(root).__name__}: {root}")


def _outcome(result: types.CallToolResult | types.Result) -> CallOutcome:
    """把 ``call_tool`` 的返回值转成一段文本 + 错误标志。"""
    content = getattr(result, "content", None) or ()
    parts: list[str] = []
    kinds: list[str] = []
    for item in content:
        kind = str(getattr(item, "type", "unknown"))
        kinds.append(kind)
        if isinstance(item, types.TextContent):
            parts.append(item.text)
        elif isinstance(item, types.ImageContent):
            parts.append(f"[图片内容：{item.mime_type}，本版未展开]")
        elif isinstance(item, types.EmbeddedResource):
            parts.append("[嵌入资源，本版未展开]")
        else:
            parts.append(f"[{kind} 内容，本版未展开]")
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        # Servers may return a short text summary alongside the actual structured
        # payload. Keep both, but avoid duplicating the JSON compatibility text.
        represented = False
        for item in content:
            if isinstance(item, types.TextContent):
                if structured == {"result": item.text}:
                    represented = True
                    break
                try:
                    parsed = json.loads(item.text)
                    represented = parsed == structured or {"result": parsed} == structured
                except (ValueError, TypeError):
                    continue
                if represented:
                    break
        if not represented:
            parts.append(json.dumps(structured, ensure_ascii=False, separators=(",", ":")))
    text = "\n".join(part for part in parts if part)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n[MCP 返回过长：原文 {len(text)} 字符，已截断]"
    return CallOutcome(
        text=text,
        is_error=bool(getattr(result, "is_error", False)),
        content_kinds=tuple(kinds),
    )


class ServerWorker:
    """一台 server 的连接。**不是线程安全的调用方直觉**：调用会串行排队。"""

    def __init__(
        self,
        name: str,
        spec: McpServerSpec,
        *,
        startup_timeout: float,
        call_timeout: float,
    ) -> None:
        self._name = name
        self._spec = spec
        self._startup_timeout = startup_timeout
        self._call_timeout = call_timeout
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_Request | None] | None = None
        self._ready = threading.Event()
        self._failure: McpFailure | None = None
        self._tools: tuple[dict[str, Any], ...] = ()
        self._closing = threading.Event()
        self._cancel_scope: anyio.CancelScope | None = None
        self._pending: set[concurrent.futures.Future[CallOutcome]] = set()
        self._pending_lock = threading.Lock()

    # --- 对外（同步）---------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def alive(self) -> bool:
        return (self._thread is not None and self._thread.is_alive() and self._ready.is_set()
                and self._failure is None and not self._closing.is_set())

    def start(self) -> tuple[dict[str, Any], ...]:
        """起进程并完成握手 + 首次 ``tools/list``。

        Returns:
            原始工具条目（``name`` / ``description`` / ``inputSchema``）。

        Raises:
            McpFailure: 启动失败；``kind`` 说明下一步该做什么。
        """
        if self._thread is not None:
            raise McpFailure(KIND_NOT_CONNECTED, f"{self._name} 已经启动过（重复 start）")
        thread = threading.Thread(target=self._run, name=f"mcp-{self._name}", daemon=True)
        self._thread = thread
        thread.start()
        # loop 内的握手有自己的超时；这里多给一点余量，覆盖进程创建与 import 开销。
        if not self._ready.wait(timeout=self._startup_timeout + _WAIT_SLACK_SECONDS):
            raise McpFailure(
                KIND_STARTUP_TIMEOUT,
                f"{self._name} 启动超时（{self._startup_timeout:g} 秒内没有完成握手）。",
            )
        if self._failure is not None:
            raise self._failure
        return self._tools

    def call_tool(self, tool: str, arguments: Mapping[str, Any]) -> CallOutcome:
        """调用一个工具。

        Raises:
            McpFailure: 超时、进程退出或协议错误。**不重试**。
        """
        if self._failure is not None:
            raise self._failure
        if not self.alive:
            raise McpFailure(KIND_NOT_CONNECTED, f"{self._name} 尚未连接。")
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or not self._ready.is_set():
            raise McpFailure(KIND_NOT_CONNECTED, f"{self._name} 尚未连接。")
        request = _Request(tool=tool, arguments=arguments, future=concurrent.futures.Future())
        with self._pending_lock:
            self._pending.add(request.future)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, request)
        except RuntimeError as exc:
            raise McpFailure(KIND_SERVER_EXITED, f"{self._name} 的连接已关闭：{exc}") from exc
        try:
            return request.future.result(timeout=self._call_timeout + _WAIT_SLACK_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            request.future.cancel()
            raise McpFailure(
                KIND_CALL_TIMEOUT, f"{self._name} 的 {tool} 调用超时（{self._call_timeout:g} 秒）。"
            ) from exc
        finally:
            with self._pending_lock:
                self._pending.discard(request.future)

    def close(self, *, timeout: float = 5.0) -> None:
        """请求连接 task 退出并等待线程结束。

        退出是**尽力而为**：线程仍在跑时只等 ``timeout`` 就返回（daemon 线程随进程退出）。
        取消连接 task 后，由 SDK 的 stdio 上下文回收进程树（Windows Job Object）。
        超过等待上限仍保留线程引用，调用方可以再次 close 等待。
        """
        self._closing.set()
        loop = self._loop
        scope = self._cancel_scope
        if loop is not None and scope is not None:
            try:
                loop.call_soon_threadsafe(scope.cancel)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        # 保留仍存活的线程引用；alive 不能靠丢掉引用伪装为 False。

    # --- 线程内（async）------------------------------------------------------

    def _run(self) -> None:
        try:
            with open(os.devnull, "w") as errlog:
                asyncio.run(self._serve(errlog))
        except BaseException as exc:  # noqa: BLE001 — 线程内任何异常都必须变成可展示的失败
            if not self._closing.is_set():
                self._failure = self._describe(exc, ready=self._ready.is_set())
        finally:
            self._ready.set()
            with self._pending_lock:
                for future in self._pending:
                    if not future.done():
                        future.set_exception(McpFailure(KIND_SERVER_EXITED, f"{self._name} 的连接已关闭。"))

    def _describe(self, exc: BaseException, *, ready: bool) -> McpFailure:
        failure = _describe_failure(exc, name=self._name, ready=ready)
        message = failure.message
        for secret in self._spec.env.values():
            if secret:
                message = message.replace(secret, "[已隐藏]")
        return McpFailure(failure.kind, message)

    def _server_parameters(self) -> StdioServerParameters:
        # ``npx`` 在 Windows 上是 ``npx.cmd``：先按 PATH 解析成绝对路径，找不到就原样交给
        # SDK，让它以"命令不存在"的形式失败（那是准确的事实，不该在这里编一个路径）。
        resolved = shutil.which(self._spec.command) or self._spec.command
        env = dict(self._spec.env) or None
        return StdioServerParameters(
            command=resolved, args=list(self._spec.args), env=env
        )

    async def _serve(self, errlog: TextIO) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        params = self._server_parameters()
        with anyio.CancelScope() as scope:
            self._cancel_scope = scope
            if self._closing.is_set():
                return
            # 第三方 stderr 可能包含凭据；不直接打印到宿主日志。
            try:
                async with (
                    stdio_client(params, errlog=errlog) as (read_stream, write_stream),
                    ClientSession(read_stream, write_stream) as session,
                ):
                    async def initialize() -> None:
                        await session.initialize()
                        self._tools = await self._list_all(session)
                    await asyncio.wait_for(initialize(), timeout=self._startup_timeout)
                    self._ready.set()
                    await self._serve_requests(session)
            except BaseException as exc:
                if not self._closing.is_set():
                    raise self._describe(exc, ready=self._ready.is_set()) from exc
                raise

    async def _list_all(self, session: ClientSession) -> tuple[dict[str, Any], ...]:
        collected: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            for tool in result.tools:
                collected.append(
                    {
                        "name": tool.name,
                        "description": tool.description or "",
                        "inputSchema": tool.input_schema or {},
                    }
                )
            cursor = result.next_cursor
            if not cursor:
                return tuple(collected)
        raise McpFailure(KIND_PROTOCOL_ERROR, f"{self._name} 的工具清单超过分页上限。")

    async def _serve_requests(self, session: ClientSession) -> None:
        assert self._queue is not None
        while True:
            request = await self._queue.get()
            if request is None:
                return
            if request.future.done():
                continue
            try:
                outcome = await asyncio.wait_for(
                    session.call_tool(request.tool, dict(request.arguments)),
                    timeout=self._call_timeout,
                )
            except asyncio.TimeoutError:
                if request.future.done():
                    continue
                request.future.set_exception(
                    McpFailure(
                        KIND_CALL_TIMEOUT,
                        f"{self._name} 的 {request.tool} 调用超时（{self._call_timeout:g} 秒）。",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                failure = self._describe(exc, ready=True)
                if not request.future.done():
                    request.future.set_exception(failure)
                if failure.kind == KIND_SERVER_EXITED:
                    raise failure
            else:
                if not request.future.done():
                    request.future.set_result(_outcome(outcome))
