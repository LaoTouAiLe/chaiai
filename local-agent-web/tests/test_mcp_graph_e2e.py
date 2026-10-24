"""MCP 工具在**真实图**里能否通过安全门并被调用（实施文档 M6）。

真实模型那次端到端里，`mcp__lanhu__lanhu_design` 被安全门判成"不可见"而拒绝执行；
装配层的三项事实（能力集含 mcp、开关为真、`external_names` 有值）却都是对的。
这条用假模型 + 本地假 server 把整条图跑一遍，定位"到底哪一道门拦的"，
并且**不需要网络与真实模型**。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from local_agent.config import AppConfig
from local_agent.graph import builder
from local_agent.mcp.binding import McpBinding
from local_agent.mcp.config import McpServersFile
from local_agent.mcp.manager import McpManager
from local_agent.mcp.state import ServerState
from local_agent.persistence import checkpoint

FAKE_SERVER = Path(__file__).with_name("mcp_fake_server.py")


def _servers() -> McpServersFile:
    return McpServersFile.model_validate(
        {
            "mcpServers": {
                "fake": {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": [str(FAKE_SERVER)],
                }
            }
        }
    )


@pytest.mark.parametrize(("tool_name", "arguments", "expected_status", "expected_text"), [
    ("echo", {"text": "你好"}, "ok", "你好"),
    ("boom", {}, "error", "返回错误"),
])
def test_mcp_tool_passes_the_gate_and_executes(
    app_config: AppConfig, tmp_path: Path, model_factory: Any,
    tool_name: str, arguments: dict[str, Any], expected_status: str, expected_text: str,
) -> None:
    manager = McpManager(
        _servers(),
        config=app_config.mcp,
        state_provider=lambda: {"fake": ServerState(enabled=True)},
    )
    manager.start()
    try:
        manager.sync()
        assert [v.status for v in manager.snapshot().servers] == ["connected"]

        model = model_factory(
            decision="tool",
            reply="好了",
            tool_calls=[
                {"name": f"mcp__fake__{tool_name}", "args": arguments, "id": "call-mcp-1"}
            ],
        )
        context = builder.build_runtime_context(
            app_config=app_config.as_run_config(),
            model_adapter=model,
            workspace_root=tmp_path,
            agent_switches={"mcp": True},
            mcp=McpBinding(
                manager=manager, states={"fake": ServerState(enabled=True)}
            ),
        )
        graph = builder.build_chat_graph(
            checkpoint.open_checkpointer(tmp_path / "chat.sqlite")
        )
        cfg = {"configurable": {"thread_id": "mcp-gate"}}

        first = graph.invoke(
            {"mode": "tool", "user_input": "调一下 echo"}, config=cfg, context=context
        )

        # 免审批（2026-10-21）：外部工具直接执行，**不**停在审批卡上。
        assert first.get("pending_approval") is None, first.get("pending_approval")
        records = [
            record
            for record in first.get("tool_results") or []
            if str(record.get("tool_name", "")).startswith("mcp__")
        ]
        assert records, first.get("tool_results")
        assert [record["status"] for record in records] == [expected_status], records[0]
        assert expected_text in (records[0].get("output_preview") or "")
    finally:
        manager.close()


def test_search_discovery_preserves_hyphens_and_truncated_names() -> None:
    from local_agent.graph.nodes import _extract_mcp_tool_names
    from local_agent.mcp.catalog import canonical_tool_name
    names = [canonical_tool_name("my-server", "get-page"), canonical_tool_name("s" * 64, "tool")]
    assert _extract_mcp_tool_names("\n".join(f"- {name}：说明" for name in names)) == names


def test_deferred_search_binds_the_tool_in_the_same_task_and_reaches_completion(app_config, tmp_path, model_factory):
    manager = McpManager(_servers(), config=app_config.mcp.model_copy(update={"max_direct_tools": 0}),
                         state_provider=lambda: {"fake": ServerState(enabled=True)})
    app_config.memory.long_term.enabled = False
    app_config.memory.medium_term.enabled = False
    try:
        manager.sync()
        model = model_factory(decision="tool", reply="已完成检索并调用", decision_tool_batches=[
            [{"name": "mcp_tool_search", "args": {"query": "echo"}, "id": "search"}],
            [{"name": "mcp__fake__echo", "args": {"text": "same-task"}, "id": "echo"}],
        ])
        context = builder.build_runtime_context(app_config=app_config.as_run_config(), model_adapter=model,
            workspace_root=tmp_path, agent_switches={"mcp": True}, mcp=McpBinding(manager=manager))
        assert "mcp__fake__echo" not in [tool.name for tool in context.tool_registry.tools_for("tool", context.agent_capabilities)]
        graph = builder.build_chat_graph(checkpoint.open_checkpointer(tmp_path / "deferred.sqlite"))
        cfg = {"configurable": {"thread_id": "deferred"}}
        result = graph.invoke({"mode": "tool", "user_input": "检索 echo 然后调用"}, config=cfg, context=context)
        # 免审批（2026-10-21）：两批工具在同一个任务里直接跑完，不再停在审批卡上。
        assert result["task_status"] == "completed"
        assert "mcp__fake__echo" in result["discovered_mcp_tools"]
        assert "mcp__fake__echo" in model.bound_tools
        echo = [r for r in result["tool_results"] if r.get("tool_name") == "mcp__fake__echo"]
        assert echo and echo[0]["status"] == "ok", echo
        records = [row for row in result["tool_results"] if row["tool_name"] == "mcp__fake__echo"]
        assert records[0]["status"] == "ok"
        assert "same-task" in records[0]["output_preview"]
    finally:
        manager.close()
