"""能力与 Agent Profile（架构 §6.9、§16.1；T50）。

这组用例守住三件事：

1. **既有行为不变**：默认 Profile（``coder``）与 ``chat`` 模式的工具集和顺序与 v0.4 逐项一致；
2. **可见性即边界**：只读 Profile 拿不到写类工具与 Shell，且未登记的能力/工具不会静默通过；
3. **配置错误启动即失败**：未知名字、空列表、重复能力、覆盖内置名都在配置阶段报错。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from local_agent.capabilities import (
    CAPABILITY_TOOLS,
    DEFAULT_PROFILE,
    MODE_CAPABILITIES,
    PROFILES,
    capability_names,
    profile_names,
    tool_names_for,
)
from local_agent.config import AppConfig, ConfigError, load_config
from local_agent.exceptions import LocalAgentError
from local_agent.graph.state import RuntimeContext
from local_agent.reflection.reflector import PLAN_DRAFT_MARKER, plan_draft_instruction
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry

#: v0.4 ``tool`` 模式的工具集（**含顺序**）：T50 的硬约束是它一个都不能变——
#: 顺序即绑定顺序，变化会让供应商前缀缓存整段失效（架构 §6.9）。
V04_TOOL_ORDER = (
    "update_plan",
    "list_dir",
    "read_file",
    "grep",
    "glob",
    "edit_file",
    "write_file",
    "delete_file",
    "run_shell",
)

#: v0.4 ``chat`` 模式：闲聊也能列目录与读文件，是既有行为，v0.5 不改。
V04_CHAT_ORDER = ("list_dir", "read_file")


def _registry(app_config: AppConfig, workspace: Path) -> ToolRegistry:
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    return ToolRegistry.build(policy)


def _names(tools: Sequence[BaseTool]) -> list[str]:
    return [tool.name for tool in tools]


def test_default_profile_reproduces_v04_tool_order(app_config: AppConfig, tmp_path: Path) -> None:
    """默认 Profile 与 chat 模式的绑定结果必须与 v0.4 逐项一致（含顺序）。"""
    registry = _registry(app_config, tmp_path)

    assert _names(registry.tools_for("chat")) == list(V04_CHAT_ORDER)
    assert _names(registry.tools_for("tool")) == list(V04_TOOL_ORDER)
    assert _names(registry.tools_for("tool", PROFILES[DEFAULT_PROFILE])) == list(V04_TOOL_ORDER)


def test_builtin_profiles_are_declared_read_only_subsets(tmp_path: Path) -> None:
    """三档内置 Profile 的能力集合（名字固定，配置与验收都用它们）。"""
    assert PROFILES["coder"] == ("plan", "files_read", "code_search", "files_write", "shell")
    assert PROFILES["researcher"] == ("plan", "files_read", "code_search", "web")
    assert PROFILES["reviewer"] == ("files_read", "code_search")
    # 最小权限用**显式枚举**表达，不假设"researcher 一定是 coder 的子集"：T53 起 researcher 多了 `web`
    # （coder 没有），两者是各取所需，而不是包含关系。
    assert set(PROFILES["reviewer"]) < set(PROFILES["researcher"])
    assert set(PROFILES["reviewer"]) < set(PROFILES["coder"])
    assert "web" in PROFILES["researcher"] and "web" not in PROFILES["coder"]


def test_only_read_tools_are_visible_to_read_only_profiles(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """只读 Profile 看不到写类工具与 Shell——可见性是边界的第一道。"""
    registry = _registry(app_config, tmp_path)

    researcher = _names(registry.tools_for("tool", PROFILES["researcher"]))
    reviewer = _names(registry.tools_for("tool", PROFILES["reviewer"]))

    assert researcher == [
        "update_plan",
        "list_dir",
        "read_file",
        "grep",
        "glob",
        # T53：Web 能力只给 researcher（架构 §16.3）
        "web_search",
        "web_fetch",
    ]
    assert reviewer == ["list_dir", "read_file", "grep", "glob"]
    for forbidden in ("edit_file", "write_file", "delete_file", "run_shell"):
        assert forbidden not in researcher
        assert forbidden not in reviewer


def test_tool_order_is_stable_across_calls_and_profiles(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """同一输入恒得到同一序列；切换 Profile 不影响其它 Profile 的结果。"""
    registry = _registry(app_config, tmp_path)

    first = _names(registry.tools_for("tool", PROFILES["coder"]))
    second = _names(registry.tools_for("tool", PROFILES["coder"]))
    assert first == second == list(V04_TOOL_ORDER)

    assert _names(registry.tools_for("tool", PROFILES["reviewer"])) == [
        "list_dir",
        "read_file",
        "grep",
        "glob",
    ]
    # 再切回来仍是原顺序：顺序由常量决定，不受访问顺序影响
    assert _names(registry.tools_for("tool", PROFILES["coder"])) == first


def test_capability_table_covers_registered_tools_exactly(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """能力表与已实现工具**一一对应**：不重不漏。"""
    registry = _registry(app_config, tmp_path)
    declared = [name for names in CAPABILITY_TOOLS.values() for name in names]

    assert len(declared) == len(set(declared)), "一个工具不能归属多个能力"
    assert set(declared) == set(registry.names())


def test_registry_construction_fails_when_a_tool_is_unregistered(
    app_config: AppConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已实现但未登记能力归属 → 构造注册表即失败，不静默少给工具。"""
    trimmed = {name: tools for name, tools in CAPABILITY_TOOLS.items() if name != "shell"}
    monkeypatch.setattr("local_agent.tools.tool_registry.CAPABILITY_TOOLS", trimmed)
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    with pytest.raises(LocalAgentError) as excinfo:
        ToolRegistry.build(policy)

    assert "run_shell" in str(excinfo.value)


def test_unknown_mode_or_capability_is_not_silently_empty(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """未知模式/能力抛错，而不是"一个工具都没有"——静默空集会让模型看起来没能力可用。"""
    registry = _registry(app_config, tmp_path)

    with pytest.raises(KeyError):
        tool_names_for("nope")
    with pytest.raises(KeyError):
        registry.tools_for("tool", ("nope",))


def test_profiles_config_defaults_and_custom_profile() -> None:
    """默认配置与自定义 Profile 的解析。"""
    default = AppConfig()
    assert default.profiles.active == DEFAULT_PROFILE
    assert default.profiles.capabilities() == PROFILES[DEFAULT_PROFILE]

    custom = AppConfig.model_validate(
        {"profiles": {"active": "docs", "custom": {"docs": ["files_read", "code_search"]}}}
    )
    assert custom.profiles.capabilities() == ("files_read", "code_search")
    assert tool_names_for("tool", custom.profiles.capabilities()) == (
        "list_dir",
        "read_file",
        "grep",
        "glob",
    )


@pytest.mark.parametrize(
    ("profiles", "expected", "lists_choices"),
    [
        # 名字写错时，消息里必须列出可选项，否则用户只能靠猜（`lists_choices`）
        ({"active": "nope"}, "profiles.active=nope", True),
        ({"custom": {"coder": ["files_read"]}}, "不能覆盖内置 Profile", True),
        ({"custom": {"docs": ["nope"]}}, "未登记的能力", True),
        # 空列表与重复是"就地能改"的错，不需要再列一遍全部名字
        ({"custom": {"docs": []}}, "能力列表不能为空", False),
        ({"custom": {"docs": ["files_read", "files_read"]}}, "能力重复", False),
    ],
)
def test_profiles_config_rejects_bad_input(
    profiles: dict[str, object], expected: str, lists_choices: bool
) -> None:
    """配错名字/列表就失败：静默忽略会变成"看着配了、实际用默认档"。"""
    with pytest.raises(ValidationError) as excinfo:
        AppConfig.model_validate({"profiles": profiles})

    message = str(excinfo.value)
    assert expected in message
    if lists_choices:
        assert profile_names()[0] in message or capability_names()[0] in message


def test_load_config_reports_unknown_active_profile(tmp_path: Path) -> None:
    """走真实加载路径：未知 Profile 名在配置阶段失败，消息里列出可选项。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("profiles:\n  active: nope\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path)

    message = str(excinfo.value)
    assert "profiles.active" in message
    assert DEFAULT_PROFILE in message


def test_runtime_context_defaults_keep_existing_callers_unchanged() -> None:
    """``RuntimeContext`` 的默认 Profile 就是 v0.4 的工具集：既有构造点不传也不变。"""
    defaults = {field.name: field.default for field in fields(RuntimeContext)}

    assert defaults["agent_profile"] == DEFAULT_PROFILE
    assert defaults["agent_capabilities"] == PROFILES[DEFAULT_PROFILE]
    assert MODE_CAPABILITIES["tool"] == PROFILES[DEFAULT_PROFILE]


def test_plan_draft_instruction_follows_the_profile() -> None:
    """计划草拟提示的工具清单随 Profile 变化：只读 Profile 不得被承诺 update_plan。"""
    coder = plan_draft_instruction(tool_names_for("tool", PROFILES["coder"]))
    reviewer = plan_draft_instruction(tool_names_for("tool", PROFILES["reviewer"]))
    default = plan_draft_instruction()

    assert PLAN_DRAFT_MARKER in coder and PLAN_DRAFT_MARKER in reviewer
    assert "update_plan" in coder
    assert "update_plan" not in reviewer
    assert "grep、glob 定位" in reviewer
    assert default == coder, "无参数时的默认指令应等于默认 Profile 的指令"
