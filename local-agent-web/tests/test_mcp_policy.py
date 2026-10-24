"""MCP 工具的安全门与可见性（实施文档 M3）。

两条边界必须同时成立，缺一条就等于开了条静默通道：

1. **可见性**：``mcp`` 能力关着、或名字不是本轮已知的，一律不可见（不是"没告诉模型"，是"不能执行"）。
2. **审批**：外部工具一律人工批准——它的副作用在本机之外，server 自述的"只读"不能当授权。
"""

from __future__ import annotations

from pathlib import Path

from local_agent.capabilities import is_visible
from local_agent.config import AppConfig
from local_agent.security.approval import action_fingerprint
from local_agent.security.policy import SecurityPolicy

CAPS_WITH_MCP = ("files_read", "mcp")
CAPS_WITHOUT_MCP = ("files_read",)
TOOL = "mcp__fake__echo"


def _policy(
    app_config: AppConfig, workspace: Path, *, allow_result_spill: bool = True
) -> SecurityPolicy:
    return SecurityPolicy.from_config(
        app_config,
        workspace_root=workspace,
        environ={},
        allow_result_spill=allow_result_spill,
    )


def test_mcp_tool_runs_without_approval(app_config: AppConfig, tmp_path: Path) -> None:
    """免审批（2026-10-21 按用户决定改）。

    server 的 ``command`` 是用户亲手写进 ``mcp.json`` 的，那就是授权；调用它暴露的工具不是
    一次新的决定。早先"一律审批"的实测结果是连着点 3~5 次卡，人很快就不再读内容直接点批准——
    那比不审批更糟，因为它把审批变成了仪式。
    """
    decision = _policy(app_config, tmp_path).classify_tool(TOOL, {})

    assert decision.allowed is True
    assert decision.requires_approval is False
    assert "外部" in decision.reason


def test_readonly_subtask_cannot_call_mcp(app_config: AppConfig, tmp_path: Path) -> None:
    """只读子任务拒绝而不是审批：没有批准能把它放行。"""
    decision = _policy(app_config, tmp_path, allow_result_spill=False).classify_tool(TOOL, {})

    assert decision.allowed is False
    assert decision.requires_approval is False


def test_search_tool_is_read_only(app_config: AppConfig, tmp_path: Path) -> None:
    """``mcp_tool_search`` **不是**外部工具：它只查本机目录，不该被当成 ``mcp__`` 前缀拦。

    这条来自真实端到端：模型搜工具时被兜底拒绝（"尚未接入或不在放行清单内"），
    因为白名单里漏了它。漏一个名字的后果是"检索入口本身不可用"——整个 Deferred 档失效。
    """
    decision = _policy(app_config, tmp_path).classify_tool("mcp_tool_search", {"query": "x"})

    assert decision.allowed is True
    assert decision.requires_approval is False


def test_unknown_non_mcp_tool_still_fails_closed(app_config: AppConfig, tmp_path: Path) -> None:
    """mcp__ 前缀分支不能顺手把"任何未登记工具"也放过去。"""
    decision = _policy(app_config, tmp_path).classify_tool("some_unregistered_tool", {})

    assert decision.allowed is False
    assert decision.requires_approval is False
    assert "尚未接入" in decision.reason


def test_visibility_requires_the_capability() -> None:
    assert is_visible("tool", CAPS_WITH_MCP, TOOL, external_names=(TOOL,)) is True
    assert is_visible("tool", CAPS_WITHOUT_MCP, TOOL, external_names=(TOOL,)) is False


def test_visibility_requires_a_known_name() -> None:
    """能力开着但名字不在本轮目录里 → 不可见（防"报个名字就能跑"）。"""
    assert is_visible("tool", CAPS_WITH_MCP, TOOL, external_names=()) is False
    assert is_visible("tool", CAPS_WITH_MCP, TOOL, external_names=("mcp__other__x",)) is False


def test_chat_mode_never_sees_mcp() -> None:
    """闲聊固定只读子集，与 tool_names_for 的 chat 分支同源。"""
    assert is_visible("chat", CAPS_WITH_MCP, TOOL, external_names=(TOOL,)) is False


def test_cancelled_is_invisible() -> None:
    assert (
        is_visible("tool", CAPS_WITH_MCP, TOOL, cancelled=True, external_names=(TOOL,)) is False
    )


def test_static_tools_still_visible_without_external_names() -> None:
    assert is_visible("tool", CAPS_WITH_MCP, "read_file") is True


def test_fingerprint_separates_servers(app_config: AppConfig, tmp_path: Path) -> None:
    """同名工具在不同 server 上是两个动作，指纹不能撞。"""
    policy = _policy(app_config, tmp_path)

    first = action_fingerprint("mcp__a__lookup", {"q": "x"}, policy=policy)
    second = action_fingerprint("mcp__b__lookup", {"q": "x"}, policy=policy)

    assert first != second


def test_fingerprint_changes_with_arguments(app_config: AppConfig, tmp_path: Path) -> None:
    policy = _policy(app_config, tmp_path)

    first = action_fingerprint(TOOL, {"text": "a"}, policy=policy)
    second = action_fingerprint(TOOL, {"text": "b"}, policy=policy)

    assert first != second
