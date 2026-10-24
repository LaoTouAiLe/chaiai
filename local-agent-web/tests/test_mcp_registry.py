"""注册表对动态外部工具的接纳（实施文档 M3）。

闭集开口必须**只开一条缝**，所以这里同时验证"能进来"和"进不来的三种情况"：

- 进来的：带 ``mcp__`` 前缀、由装配层从目录给出；
- 进不来的：不带前缀、与内置同名、与另一个外部工具同名。

再加一条容易做错的语义：``Deferred`` 档**不绑给模型**但**可以执行**——检索到的工具必须调得动。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool

from local_agent.capabilities import is_visible
from local_agent.config import AppConfig
from local_agent.exceptions import LocalAgentError
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry

CAPS_WITH_MCP = ("files_read", "mcp")
CAPS_WITHOUT_MCP = ("files_read",)


def _policy(app_config: AppConfig, workspace: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})


def _external(name: str) -> BaseTool:
    def _run(**kwargs: Any) -> str:
        return "ok"

    return StructuredTool(
        name=name,
        description="测试用外部工具",
        args_schema={"type": "object", "properties": {}},
        func=_run,
    )


def _names(tools: Sequence[BaseTool]) -> list[str]:
    return [tool.name for tool in tools]


def test_external_tools_are_registered_and_listed(app_config: AppConfig, tmp_path: Path) -> None:
    tool = _external("mcp__fake__echo")
    registry = ToolRegistry.build(
        _policy(app_config, tmp_path),
        git_available=True,
        mcp_tools=[tool],
        mcp_direct=[tool.name],
    )

    assert tool.name in registry.names()
    assert "mcp_tool_search" in registry.names()
    assert registry.external_names() == (tool.name,)


def test_mcp_capability_off_hides_both_search_and_tools(
    app_config: AppConfig, tmp_path: Path
) -> None:
    tool = _external("mcp__fake__echo")
    registry = ToolRegistry.build(
        _policy(app_config, tmp_path),
        git_available=True,
        mcp_tools=[tool],
        mcp_direct=[tool.name],
    )

    bound = _names(registry.tools_for("tool", CAPS_WITHOUT_MCP))

    assert "mcp_tool_search" not in bound
    assert tool.name not in bound


def test_capability_on_binds_search_and_direct_tools(
    app_config: AppConfig, tmp_path: Path
) -> None:
    direct = _external("mcp__fake__echo")
    deferred = _external("mcp__fake__other")
    registry = ToolRegistry.build(
        _policy(app_config, tmp_path),
        git_available=True,
        mcp_tools=[direct, deferred],
        mcp_direct=[direct.name],
    )

    bound = _names(registry.tools_for("tool", CAPS_WITH_MCP))

    assert "mcp_tool_search" in bound
    assert direct.name in bound
    # Deferred 不绑（省提示预算），但可执行（否则检索成了摆设）。
    assert deferred.name not in bound
    assert deferred.name in registry.external_names()
    assert is_visible("tool", CAPS_WITH_MCP, deferred.name, external_names=registry.external_names())


def test_deferred_tool_is_not_bound_even_when_capability_on(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """``mcp_direct`` 为空时，绑定结果里只有检索工具。"""
    tool = _external("mcp__fake__echo")
    registry = ToolRegistry.build(
        _policy(app_config, tmp_path), git_available=True, mcp_tools=[tool], mcp_direct=[]
    )

    bound = _names(registry.tools_for("tool", CAPS_WITH_MCP))

    assert bound[-1] == "mcp_tool_search"
    assert tool.name not in bound


def test_external_tool_without_prefix_is_rejected(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with pytest.raises(LocalAgentError, match="mcp__"):
        ToolRegistry.build(
            _policy(app_config, tmp_path),
            git_available=True,
            mcp_tools=[_external("evil_tool")],
        )


def test_external_tool_colliding_with_builtin_is_rejected(
    app_config: AppConfig, tmp_path: Path
) -> None:
    with pytest.raises(LocalAgentError, match="重复"):
        ToolRegistry.build(
            _policy(app_config, tmp_path),
            git_available=True,
            mcp_tools=[_external("mcp__fake__echo"), _external("mcp__fake__echo")],
        )


def test_search_tool_is_always_constructed(app_config: AppConfig, tmp_path: Path) -> None:
    """能力关掉时工具仍在（只是不可见）——覆盖校验要求"登记了必须实现"。"""
    registry = ToolRegistry.build(_policy(app_config, tmp_path), git_available=True)

    assert "mcp_tool_search" in registry.names()
    assert registry.external_names() == ()
