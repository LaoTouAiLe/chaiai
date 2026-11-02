"""远程 MCP（Streamable HTTP）的连接层契约（2026-11-02）。

守住三件事：

1. **传输只在一处分叉**：``http`` 走 ``streamable_http_client``，``stdio`` 走子进程；
   两者共用握手、列工具、调用与失败归类（这里用替身证明"走了哪条"）。
2. **请求头只进 HTTP 客户端**：它通常带 token，不能进日志、错误文案或 DTO。
3. **失败文案指到端点**：远程握手失败的原因就那么几类，不给端点等于让用户猜自己配的地址有没有生效。
"""

from __future__ import annotations

import asyncio
import io
from contextlib import asynccontextmanager
from typing import Any

import pytest

from local_agent.mcp import client as client_module
from local_agent.mcp.client import ServerWorker
from local_agent.mcp.config import McpServerSpec
from local_agent.mcp.errors import KIND_HANDSHAKE_FAILED, McpFailure

HTTP_SPEC = McpServerSpec(
    type="http",
    url="https://example.com/mcp",
    headers={"Authorization": "Bearer sk-secret-token"},
)


def _worker(spec: McpServerSpec) -> ServerWorker:
    return ServerWorker("remote", spec, startup_timeout=1.0, call_timeout=1.0)


def test_http_transport_uses_the_streamable_client_with_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeClient:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    def fake_create_client(*, headers: dict[str, str] | None = None, **kwargs: object) -> FakeClient:
        seen["headers"] = headers
        return FakeClient()

    @asynccontextmanager
    async def fake_streamable(url: str, *, http_client: object = None, **kwargs: object):
        seen["url"] = url
        seen["http_client"] = http_client
        yield ("read", "write")

    def fail_stdio(*args: object, **kwargs: object):
        raise AssertionError("http 传输不该启动本机子进程")

    monkeypatch.setattr(client_module, "create_mcp_http_client", fake_create_client)
    monkeypatch.setattr(client_module, "streamable_http_client", fake_streamable)
    monkeypatch.setattr(client_module, "stdio_client", fail_stdio)

    async def run() -> Any:
        async with _worker(HTTP_SPEC)._transport(io.StringIO()) as streams:
            return streams

    assert asyncio.run(run()) == ("read", "write")
    assert seen["url"] == "https://example.com/mcp"
    assert seen["headers"] == {"Authorization": "Bearer sk-secret-token"}
    assert seen["http_client"] is not None


def test_stdio_transport_still_spawns_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_stdio(params: object, *, errlog: object = None):
        seen["params"] = params
        yield ("read", "write")

    def fail_http(*args: object, **kwargs: object):
        raise AssertionError("stdio 传输不该创建 HTTP 客户端")

    monkeypatch.setattr(client_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(client_module, "create_mcp_http_client", fail_http)

    async def run() -> Any:
        async with _worker(McpServerSpec(command="npx"))._transport(io.StringIO()) as streams:
            return streams

    assert asyncio.run(run()) == ("read", "write")
    assert getattr(seen["params"], "command", "") != ""


def test_header_values_are_redacted_from_failure_messages() -> None:
    """异常文本里带出一个 Bearer token，等于把它写进了页面与日志。"""
    worker = _worker(HTTP_SPEC)

    failure = worker._describe(
        McpFailure(KIND_HANDSHAKE_FAILED, "TLS 失败：Authorization=Bearer sk-secret-token"),
        ready=False,
    )

    assert "sk-secret-token" not in failure.message
    assert "[已隐藏]" in failure.message


def test_http_handshake_failure_names_the_endpoint() -> None:
    worker = _worker(HTTP_SPEC)

    failure = worker._describe(McpFailure(KIND_HANDSHAKE_FAILED, "连接被拒绝"), ready=False)

    assert "https://example.com/mcp" in failure.message
    # 别把凭据顺手带出来。
    assert "sk-secret-token" not in failure.message
