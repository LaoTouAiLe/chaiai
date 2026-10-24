"""装配层把 MCP 目录接进注册表（实施文档 M4）。

用**假 manager**（满足 ``McpManagerLike``）而不是真连接：这一层要回答的是"目录与开关怎么变成
工具表"，真连接的行为已经由 ``test_mcp_client.py`` / ``test_mcp_manager.py`` 守住了。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel

from local_agent.config import AppConfig, apply_capability_gates
from local_agent.graph.builder import build_runtime_context
from local_agent.mcp.binding import McpBinding
from local_agent.mcp.catalog import ExposureRow, ToolEntry, ToolExposure
from local_agent.mcp.state import ServerState


def _entry(name: str) -> ToolEntry:
    return ToolEntry(
        server="fake",
        name=name,
        canonical_name=f"mcp__fake__{name}",
        description=f"{name} 工具",
        input_schema={"type": "object", "properties": {}},
        schema_bytes=20,
    )


class _FakeManager:
    def __init__(self, entries: tuple[ToolEntry, ...], direct: tuple[str, ...]) -> None:
        self._entries = entries
        self._snapshot = SimpleNamespace(
            servers=(),
            rows=tuple(ExposureRow(entry, ToolExposure.DIRECT if entry.canonical_name in direct
                                   else ToolExposure.DEFERRED) for entry in entries),
            direct=direct,
            searchable=entries,
            entries=lambda: entries,
        )

    def snapshot(self) -> Any:
        return self._snapshot

    def call_tool(self, server: str, tool: str, arguments: Mapping[str, Any]) -> Any:
        raise AssertionError("装配阶段不该发起调用")


def _context(
    app_config: AppConfig, workspace: Path, *, switches: dict[str, bool], binding: McpBinding | None
):
    return build_runtime_context(
        app_config=app_config.as_run_config(),
        model_adapter=FakeListChatModel(responses=["ok"]),
        workspace_root=workspace,
        agent_switches=switches,
        mcp=binding,
    )


def _bound(context: Any) -> list[str]:
    return [tool.name for tool in context.tool_registry.tools_for(
        "tool", context.agent_capabilities
    )]


def test_without_binding_no_mcp_tool_is_bound(app_config: AppConfig, tmp_path: Path) -> None:
    context = _context(app_config, tmp_path, switches={"mcp": True}, binding=None)

    assert "mcp_tool_search" not in _bound(context)


def test_capability_off_keeps_mcp_out_even_with_a_binding(
    app_config: AppConfig, tmp_path: Path
) -> None:
    entries = (_entry("echo"),)
    binding = McpBinding(manager=_FakeManager(entries, ("mcp__fake__echo",)))

    context = _context(app_config, tmp_path, switches={"mcp": False}, binding=binding)

    assert "mcp_tool_search" not in _bound(context)


def test_direct_tool_is_bound_and_deferred_is_not(app_config: AppConfig, tmp_path: Path) -> None:
    entries = (_entry("echo"), _entry("other"))
    binding = McpBinding(manager=_FakeManager(entries, ("mcp__fake__echo",)))

    context = _context(app_config, tmp_path, switches={"mcp": True}, binding=binding)
    bound = _bound(context)

    assert "mcp_tool_search" in bound
    assert "mcp__fake__echo" in bound
    assert "mcp__fake__other" not in bound


def test_discovered_tools_stay_bound_across_turns(app_config: AppConfig, tmp_path: Path) -> None:
    """检索到的工具要跨轮保留，否则模型每轮都得重新搜一次。"""
    entries = (_entry("echo"), _entry("other"))
    binding = McpBinding(
        manager=_FakeManager(entries, ("mcp__fake__echo",)),
        discovered=("mcp__fake__other",),
    )

    context = _context(app_config, tmp_path, switches={"mcp": True}, binding=binding)
    bound = _bound(context)

    assert "mcp__fake__other" in bound


def test_discovered_names_that_no_longer_exist_are_ignored(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """server 删了工具之后，会话里残留的旧名字不能变成"绑定了一个不存在的东西"。"""
    entries = (_entry("echo"),)
    binding = McpBinding(
        manager=_FakeManager(entries, ("mcp__fake__echo",)),
        discovered=("mcp__fake__removed",),
    )

    context = _context(app_config, tmp_path, switches={"mcp": True}, binding=binding)

    assert "mcp__fake__removed" not in _bound(context)


def test_gate_follows_the_switch(app_config: AppConfig) -> None:
    """``config.yaml`` 的 ``mcp.enabled`` 只是出厂默认，开关表才是运行期真源。"""
    gated_on = apply_capability_gates(app_config, {"mcp": True})
    gated_off = apply_capability_gates(app_config, {"mcp": False})

    assert gated_on.mcp.enabled is True
    assert gated_off.mcp.enabled is False


def test_server_state_defaults_to_disabled() -> None:
    assert ServerState().enabled is False


def test_hidden_discovered_tool_is_not_registered(app_config: AppConfig, tmp_path: Path) -> None:
    entry = _entry("hidden")
    manager = _FakeManager((entry,), ())
    manager._snapshot.rows = (ExposureRow(entry, ToolExposure.HIDDEN, "in_disabled_tools"),)
    context = _context(app_config, tmp_path, switches={"mcp": True}, binding=McpBinding(
        manager=manager, discovered=(entry.canonical_name,)))
    assert entry.canonical_name not in _bound(context)
    assert entry.canonical_name not in context.tool_registry.external_names()


def test_search_registers_tools_connected_after_context_was_built(app_config, tmp_path):
    manager = _FakeManager((), ())
    context = _context(app_config, tmp_path, switches={"mcp": True}, binding=McpBinding(manager=manager))
    entry = _entry("late_echo")
    manager._snapshot.rows = (ExposureRow(entry, ToolExposure.DIRECT),)
    result = context.tool_registry.get("mcp_tool_search").invoke({"query": "late_echo"})
    assert entry.canonical_name in result
    bound = context.tool_registry.tools_for("tool", context.agent_capabilities,
                                            discovered_mcp=[entry.canonical_name])
    assert entry.canonical_name in [tool.name for tool in bound]
