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
    effective_capabilities,
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

#: v0.5 T54 给默认档追加的 Git 工具（**追加在末尾**：前面的字节不变，前缀缓存前半段仍可复用）。
GIT_TOOL_ORDER = ("git_status", "git_diff", "git_log", "git_commit")


def _registry(app_config: AppConfig, workspace: Path) -> ToolRegistry:
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    # 注入确定的 git 可用性：用例不该因为"跑测试的机器上装没装 git"而结果不同。
    return ToolRegistry.build(policy, git_available=True)


def _names(tools: Sequence[BaseTool]) -> list[str]:
    return [tool.name for tool in tools]


def test_default_profile_keeps_the_v04_prefix_and_appends_git(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """默认 Profile：v0.4 的九个工具**顺序与位置都不变**，新能力追加在末尾。"""
    registry = _registry(app_config, tmp_path)

    assert _names(registry.tools_for("chat")) == list(V04_CHAT_ORDER)
    tool_mode = _names(registry.tools_for("tool"))
    # 前缀逐项相等是**成本约束**：前面的字节不变，供应商前缀缓存的前半段仍可复用（架构 §6.9）。
    assert tool_mode[: len(V04_TOOL_ORDER)] == list(V04_TOOL_ORDER)
    assert tool_mode == [*V04_TOOL_ORDER, *GIT_TOOL_ORDER]
    assert _names(registry.tools_for("tool", PROFILES[DEFAULT_PROFILE])) == tool_mode


def test_builtin_profiles_are_declared_read_only_subsets(tmp_path: Path) -> None:
    """三档内置 Profile 的能力集合（名字固定，配置与验收都用它们）。"""
    assert PROFILES["coder"] == (
        "plan",
        "files_read",
        "code_search",
        "files_write",
        "shell",
        # T54：Git 读写都进默认档（写的那一半仍要过人工审批，见 §16.4）。
        "git_read",
        "git_write",
    )
    assert PROFILES["researcher"] == ("plan", "files_read", "code_search", "web", "git_read")
    assert PROFILES["reviewer"] == ("files_read", "code_search")
    # 最小权限用**显式枚举**表达，不假设"researcher 一定是 coder 的子集"：T53 起 researcher 多了 `web`
    # （coder 没有），两者是各取所需，而不是包含关系；T54 又多了 Git 的**读**能力。
    assert set(PROFILES["reviewer"]) < set(PROFILES["researcher"])
    assert set(PROFILES["reviewer"]) < set(PROFILES["coder"])
    assert "web" in PROFILES["researcher"] and "web" not in PROFILES["coder"]
    # 读 / 写分成两个能力就是为了能这样分别表态：研究档看得到差异，但**不能提交**。
    assert "git_read" in PROFILES["researcher"] and "git_write" not in PROFILES["researcher"]


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
        # T54：Git 的**读**能力（`git_read`）也给它——看差异可以，提交不行（见下面的 forbidden）
        "git_status",
        "git_diff",
        "git_log",
    ]
    assert reviewer == ["list_dir", "read_file", "grep", "glob"]
    # `git_commit` 是写类：只读档里连"看得见"都不该有（§16.4）。
    for forbidden in ("edit_file", "write_file", "delete_file", "run_shell", "git_commit"):
        assert forbidden not in researcher
        assert forbidden not in reviewer


def test_tool_order_is_stable_across_calls_and_profiles(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """同一输入恒得到同一序列；切换 Profile 不影响其它 Profile 的结果。"""
    registry = _registry(app_config, tmp_path)

    first = _names(registry.tools_for("tool", PROFILES["coder"]))
    second = _names(registry.tools_for("tool", PROFILES["coder"]))
    assert first == second == [*V04_TOOL_ORDER, *GIT_TOOL_ORDER]

    assert _names(registry.tools_for("tool", PROFILES["reviewer"])) == [
        "list_dir",
        "read_file",
        "grep",
        "glob",
    ]
    # 再切回来仍是原顺序：顺序由常量决定，不受访问顺序影响
    assert _names(registry.tools_for("tool", PROFILES["coder"])) == first


def test_git_capability_is_gated_by_config_and_by_the_environment() -> None:
    """Git 能力进不进有效集合，取决于**配置开关**与**本机有没有 git** 两件事（架构 §16.4）。"""
    coder = PROFILES["coder"]

    enabled = effective_capabilities(coder, web_access_enabled=False, git_enabled=True)
    assert {"git_read", "git_write"} <= set(enabled)

    disabled = effective_capabilities(coder, web_access_enabled=False, git_enabled=False)
    assert not {"git_read", "git_write"} & set(disabled)
    # 关掉 Git 之后剩下的正是 v0.4 那九个工具的能力映射——"关闭新增能力后基础行为不变"。
    assert tool_names_for("tool", disabled) == V04_TOOL_ORDER

    # 默认关闭：示例配置里的开关就是 false，因此默认档与 v0.4 的工具集一致（不含 Git）。
    assert AppConfig().git.enabled is False


def test_registry_keeps_git_tools_even_when_the_environment_lacks_git(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """缺 git 时工具**仍然构造**（能力覆盖校验要求），只是能力不进有效集合、不会绑给模型。"""
    policy = SecurityPolicy.from_config(app_config, workspace_root=tmp_path, environ={})

    registry = ToolRegistry.build(policy, git_available=False)

    assert "git_status" in registry.names() and "git_commit" in registry.names()
    assert registry.git_available is False
    assert not {"git_read", "git_write"} & set(
        effective_capabilities(
            PROFILES["coder"], web_access_enabled=False, git_enabled=registry.git_available
        )
    )


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
