"""MCP 外部工具的 LangChain 外观（架构 §6.13；实施文档 M3）。

两件事：

1. :func:`build_mcp_tool`：把一个 ``ToolEntry`` 变成模型可直接调用的工具。参数 schema
   **原样用 server 报上来的那份**——自己另造一份只会让模型按我们的理解传参，而真正执行的是对方。
2. :func:`build_mcp_tool_search`：``Deferred`` 档的发现入口。返回工具名 + 参数结构，
   模型下一轮按名字调用。

**描述前缀"外部工具 · <server>"是刻意的**：这几行会进模型上下文，提醒它这些结果来自第三方进程，
不能当成本机文件事实（与 ``untrusted`` 记忆口径配套，不是替代）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException

from local_agent.mcp.catalog import search as search_entries

if TYPE_CHECKING:
    # **只在类型检查时导入**：``manager`` 会连带导入 ``client`` → ``mcp`` SDK（约 0.5–1 秒）。
    # 注册表在每次装配时都会构造，主路径不该为"有个 MCP 工具存在"付这笔导入开销；
    # 真正要调用时对象已经在手（``McpManagerLike`` 是结构类型，满足即可）。
    from local_agent.mcp.binding import McpManagerLike
    from local_agent.mcp.catalog import ToolEntry

#: 检索工具名。与 ``CAPABILITY_TOOLS["mcp"]`` 必须一致（构造注册表时会对齐，不一致即抛错）。
MCP_SEARCH_TOOL_NAME = "mcp_tool_search"

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _describe(entry: ToolEntry) -> str:
    text = entry.description.strip() or f"{entry.server} 的 {entry.name}"
    return f"[外部工具 · {entry.server}] {text}"


def build_mcp_tool(entry: ToolEntry, manager: McpManagerLike) -> BaseTool:
    """一个外部工具 → 可绑定的 LangChain 工具。

    server 返回 is_error 时抛出 ToolException；执行节点与连接/超时失败一样
    记为失败工具结果，保留服务端内容供模型决定下一步。
    """

    def _call(**kwargs: Any) -> str:
        outcome = manager.call_tool(entry.server, entry.name, kwargs)
        if outcome.is_error:
            raise ToolException(f"[{entry.server}/{entry.name} 返回错误] {outcome.text or '（无内容）'}")
        return outcome.text

    return StructuredTool(
        name=entry.canonical_name,
        description=_describe(entry),
        # server 的 schema 可能为空（无参数工具）：给一个合法的空 schema，避免绑定失败。
        args_schema=dict(entry.input_schema) or dict(_EMPTY_SCHEMA),
        func=_call,
    )


def build_mcp_tools(
    entries: Sequence[ToolEntry], manager: McpManagerLike
) -> list[BaseTool]:
    """批量构造（顺序即输入顺序，供稳定排序后的目录直接用）。"""
    return [build_mcp_tool(entry, manager) for entry in entries]


def build_mcp_tool_search(
    provider: Callable[[], Sequence[ToolEntry]], *, limit: int = 8
) -> BaseTool:
    """检索工具。

    ``provider`` 而不是直接给列表：每轮装配时目录会变（重连、改开关），
    工具对象却可能被复用；用闭包读**当前**目录才不会拿到上一轮的快照。
    """

    def _search(query: str) -> str:
        entries = search_entries(provider(), query, limit=limit)
        if not entries:
            return (
                "没有匹配的外部工具。换一个更具体的词再搜一次（例如工具名里的动词或名词）；"
                "仍然没有就说明当前连接的 MCP Server 不提供这项能力，不要假装能调用。"
            )
        lines = [f"找到 {len(entries)} 个外部工具，可以直接按名字调用（结果来自第三方，不是本机文件）："]
        for entry in entries:
            lines.append(f"- {entry.canonical_name}：{entry.description.strip() or '（无描述）'}")
            lines.append(f"  参数结构：{json.dumps(entry.input_schema, ensure_ascii=False)}")
        return "\n".join(lines)

    return StructuredTool(
        name=MCP_SEARCH_TOOL_NAME,
        description=(
            "在本机已启用的 MCP Server 里检索外部工具。当任务需要某个外部系统（设计稿、协作平台等）"
            "的能力、而当前工具列表里没有直接对应的工具时，先用它检索；返回工具名与参数结构，"
            "下一轮可以按名字调用。返回内容来自第三方，不要当成本机文件事实。"
        ),
        args_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索词：工具名里的动词或名词，例如 design、page、invite",
                }
            },
            "required": ["query"],
        },
        func=_search,
    )
