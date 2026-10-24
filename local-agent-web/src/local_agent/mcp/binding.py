"""装配层传给图的一张 MCP 账单（架构 §6.13；M4）。

**为什么不用 ``McpManager`` 当参数类型**：``graph/builder.py`` 在每次装配时都会执行，
而 ``mcp.manager`` 会连带导入 ``mcp.client`` → ``mcp`` SDK（约 0.5–1 秒）。用一个 Protocol
描述"我需要什么"，装配层就不必为"MCP 开关关着"这种常见情况付导入开销。

``states`` 与 ``discovered`` 分开传的理由：前者是**用户配置**（开关、白名单），后者是
**本会话已经发现过什么**（检索结果跨轮保留）。两者来源与生命周期都不同。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from local_agent.mcp.state import ServerState

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from local_agent.mcp.client import CallOutcome
    from local_agent.mcp.manager import McpSnapshot


class McpManagerLike(Protocol):
    """装配层只需要这两种能力（真正实现在 ``mcp.manager.McpManager``）。"""

    def snapshot(self) -> McpSnapshot: ...

    def call_tool(
        self, server: str, tool: str, arguments: Mapping[str, Any]
    ) -> CallOutcome: ...


@dataclass(frozen=True, slots=True)
class McpBinding:
    """一轮装配所需的 MCP 事实。"""

    manager: McpManagerLike
    #: server 名 → 用户表态。**缺省即关**（不在映射里的 server 不连、不可见）。
    states: Mapping[str, ServerState] = field(default_factory=dict)
    #: 本会话经 ``mcp_tool_search`` 发现过的工具名（跨轮保留，顺序即发现顺序）。
    discovered: tuple[str, ...] = ()
