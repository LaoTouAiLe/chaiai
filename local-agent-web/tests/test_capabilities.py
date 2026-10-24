"""能力与 Agent Profile（架构 §6.9、§16.1；T50）。

这组用例守住三件事：

1. **现行契约稳定**：默认 Profile 与 chat 模式的完整工具序列匹配已授权能力；
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
    CAPABILITY_LABELS,
    CAPABILITY_TOOLS,
    DEFAULT_PROFILE,
    MODE_CAPABILITIES,
    PROFILES,
    REASON_AVAILABLE,
    REASON_DISABLED,
    REASON_MISSING_DEPENDENCY,
    CapabilityStatus,
    as_switch_layer,
    capability_names,
    capability_status,
    effective_capabilities,
    profile_names,
    resolve_switches,
    tool_names_for,
)
from local_agent.config import AppConfig, ConfigError, load_config
from local_agent.exceptions import LocalAgentError
from local_agent.graph.state import RuntimeContext
from local_agent.reflection.reflector import PLAN_DRAFT_MARKER, plan_draft_instruction
from local_agent.security.policy import SecurityPolicy
from local_agent.tools.tool_registry import ToolRegistry

#: 当前核心工具序列；显式列出，避免由被测实现生成期望。
CORE_TOOL_ORDER = (
    "task_control", "update_plan", "list_dir", "read_file", "grep", "glob", "search_files",
    "edit_file", "write_file", "delete_file", "download_file", "run_shell",
    # 2026-10-21：转后台之后的两条查询/结束工具（同属 shell 能力，紧跟在 run_shell 之后）。
    "read_command", "stop_command",
    # 2026-10-22：跑一段 Python（同一 shell 能力门）。
    "execute_code",
)
CHAT_TOOL_ORDER = ("list_dir", "read_file", "web_search", "web_fetch")
GIT_TOOL_ORDER = ("git_status", "git_diff", "git_log", "git_commit")
SKILL_TOOL_ORDER = ("skill_list", "skill_load")
WEB_TOOL_ORDER = ("web_search", "web_fetch")
IMAGE_TOOL_ORDER = ("view_image",)
INTERACTIVE_TOOL_ORDER = (
    "start_shell", "read_shell", "write_shell", "stop_shell", "list_shell_sessions",
    "inspect_system", "browser",
)
BASELINE_TOOL_ORDER = (*CORE_TOOL_ORDER, *WEB_TOOL_ORDER, *IMAGE_TOOL_ORDER, *INTERACTIVE_TOOL_ORDER)
CODER_TOOL_ORDER = (
    *CORE_TOOL_ORDER, *WEB_TOOL_ORDER, *GIT_TOOL_ORDER, *SKILL_TOOL_ORDER,
    *IMAGE_TOOL_ORDER, *INTERACTIVE_TOOL_ORDER,
)


def _registry(app_config: AppConfig, workspace: Path) -> ToolRegistry:
    policy = SecurityPolicy.from_config(app_config, workspace_root=workspace, environ={})
    # 注入确定的 git 可用性：用例不该因为"跑测试的机器上装没装 git"而结果不同。
    return ToolRegistry.build(policy, git_available=True)


def _names(tools: Sequence[BaseTool]) -> list[str]:
    return [tool.name for tool in tools]


def test_default_profile_exposes_current_tools_in_stable_order(
    app_config: AppConfig, tmp_path: Path,
) -> None:
    """控制、下载和交互工具均在现行能力契约中，chat 仍保持固定只读工具集。"""
    registry = _registry(app_config, tmp_path)
    assert _names(registry.tools_for("chat")) == list(CHAT_TOOL_ORDER)
    assert _names(registry.tools_for("tool")) == list(CODER_TOOL_ORDER)
    assert _names(registry.tools_for("tool", PROFILES[DEFAULT_PROFILE])) == list(CODER_TOOL_ORDER)


def test_builtin_profiles_are_declared_read_only_subsets(tmp_path: Path) -> None:
    """三档内置 Profile 的能力集合（名字固定，配置与验收都用它们）。"""
    assert PROFILES["coder"] == (
        "task_lifecycle",
        "plan",
        "files_read",
        "code_search",
        "files_write",
        "shell",
        # 2026-10-14：联网是常态能力，三个档位都有它（放在 shell 之后 = 与 `CAPABILITY_TOOLS` 同序）。
        "web",
        # T54：Git 读写都进默认档（写的那一半仍受权限开关约束，见 §16.4）。
        "git_read",
        "git_write",
        # T55：技能清单与正文是只读的，三档都给（§16.5）。
        "skill",
        # 2026-10-16：看图（只读）。追加在末尾 = 与 `CAPABILITY_TOOLS` 同序。
        "image_read",
        "terminal", "page_control",
    )
    assert PROFILES["researcher"] == (
        "task_lifecycle", "plan", "files_read", "code_search", "web", "git_read", "skill", "image_read",
    )
    assert PROFILES["reviewer"] == ("task_lifecycle", "files_read", "code_search", "web", "skill", "image_read")
    # 最小权限用**显式枚举**表达，不假设"researcher 一定是 coder 的子集"：T54 起 researcher 多了 Git 的
    # **读**能力，两者是各取所需，而不是包含关系。
    assert set(PROFILES["reviewer"]) < set(PROFILES["researcher"])
    assert set(PROFILES["reviewer"]) < set(PROFILES["coder"])
    # 联网自 2026-10-14 起是**常态能力**：三档都有它（不再是"只有 researcher 有"）。
    assert all("web" in PROFILES[name] for name in ("coder", "researcher", "reviewer"))
    # 读 / 写分成两个能力就是为了能这样分别表态：研究档看得到差异，但**不能提交**。
    assert "git_read" in PROFILES["researcher"] and "git_write" not in PROFILES["researcher"]
    # T55：技能是只读的，只读档也该能按需读手册——所以三档都有 `skill`。
    assert all("skill" in PROFILES[name] for name in ("coder", "researcher", "reviewer"))


def test_only_read_tools_are_visible_to_read_only_profiles(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """只读 Profile 看不到写类工具与 Shell——可见性是边界的第一道。"""
    registry = _registry(app_config, tmp_path)

    researcher = _names(registry.tools_for("tool", PROFILES["researcher"]))
    reviewer = _names(registry.tools_for("tool", PROFILES["reviewer"]))

    assert researcher == [
        "task_control",
        "update_plan",
        "list_dir",
        "read_file",
        "grep",
        "glob",
        # 合并入口（2026-10-22）：只读，只读档也有它。
        "search_files",
        # 联网（2026-10-14 起常态：只读档也有它——检索抓取不改本机）
        "web_search",
        "web_fetch",
        # T54：Git 的**读**能力（`git_read`）也给它——看差异可以，提交不行（见下面的 forbidden）
        "git_status",
        "git_diff",
        "git_log",
        # T55：技能（只读）
        "skill_list",
        "skill_load",
        # 看图（2026-10-16）：只读，只读档也有它（排在技能之后 = 与 `CAPABILITY_TOOLS` 同序）。
        "view_image",
    ]
    assert reviewer == [
        "task_control",
        "list_dir",
        "read_file",
        "grep",
        "glob",
        # 合并入口（2026-10-22）：只读，只读档也有它。
        "search_files",
        # 联网也是只读能力：只读档照样能查资料（2026-10-14）。
        "web_search",
        "web_fetch",
        "skill_list",
        "skill_load",
        # 看图（2026-10-16）：只读，只读档也有它（排在技能之后 = 与 `CAPABILITY_TOOLS` 同序）。
        "view_image",
    ]
    # `git_commit` 是写类：只读档里连"看得见"都不该有（§16.4）。
    for forbidden in ("edit_file", "write_file", "delete_file", "download_file", "run_shell",
                      "git_commit", *INTERACTIVE_TOOL_ORDER):
        assert forbidden not in researcher
        assert forbidden not in reviewer


def test_tool_order_is_stable_across_calls_and_profiles(
    app_config: AppConfig, tmp_path: Path
) -> None:
    """同一输入恒得到同一序列；切换 Profile 不影响其它 Profile 的结果。"""
    registry = _registry(app_config, tmp_path)

    first = _names(registry.tools_for("tool", PROFILES["coder"]))
    second = _names(registry.tools_for("tool", PROFILES["coder"]))
    assert first == second == list(CODER_TOOL_ORDER)

    assert _names(registry.tools_for("tool", PROFILES["reviewer"])) == [
        "task_control",
        "list_dir",
        "read_file",
        "grep",
        "glob",
        # 合并入口（2026-10-22）：只读，只读档也有它。
        "search_files",
        # 联网是只读能力：只读档也有它（2026-10-14 起常态）。
        "web_search",
        "web_fetch",
        "skill_list",
        "skill_load",
        # 看图（2026-10-16）：只读，只读档也有它（排在技能之后 = 与 `CAPABILITY_TOOLS` 同序）。
        "view_image",
    ]
    # 再切回来仍是原顺序：顺序由常量决定，不受访问顺序影响
    assert _names(registry.tools_for("tool", PROFILES["coder"])) == first


def test_git_capability_is_gated_by_config_and_by_the_environment() -> None:
    """Git 能力进不进有效集合，取决于**配置开关**与**本机有没有 git** 两件事（架构 §16.4）。"""
    coder = PROFILES["coder"]

    enabled = effective_capabilities(
        coder, web_access_enabled=False, git_enabled=True, skills_enabled=False
    )
    assert {"git_read", "git_write"} <= set(enabled)

    disabled = effective_capabilities(
        coder, web_access_enabled=False, git_enabled=False, skills_enabled=False
    )
    assert not {"git_read", "git_write"} & set(disabled)
    # 关掉 Git 之后剩下的是**基线工具集**（现行核心工具 + 联网/看图/交互工具）——"关闭新增能力后基础行为不变"，
    # 只是这条"基线"自 2026-10-14 起含联网（它不再是扩展能力）。
    assert tool_names_for("tool", disabled) == BASELINE_TOOL_ORDER

    # 默认关闭：示例配置里的开关就是 false，因此默认配置不包含 Git。
    assert AppConfig().git.enabled is False


def test_skill_capability_is_gated_by_config_only() -> None:
    """技能能力只看**配置开关**（不像 git 还要看本机有没有可执行文件）；默认关闭。"""
    coder = PROFILES["coder"]

    on = effective_capabilities(
        coder, web_access_enabled=False, git_enabled=False, skills_enabled=True
    )
    assert "skill" in on
    off = effective_capabilities(
        coder, web_access_enabled=False, git_enabled=False, skills_enabled=False
    )
    assert "skill" not in off
    # 关掉它之后，默认档正好回到**基线工具集**（现行核心工具 + 联网/看图/交互工具）。
    assert tool_names_for("tool", off) == BASELINE_TOOL_ORDER
    assert AppConfig().skills.enabled is False


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
            PROFILES["coder"],
            web_access_enabled=False,
            git_enabled=registry.git_available,
            skills_enabled=False,
        )
    )


def _status_rows(
    *,
    capabilities: Sequence[str] | None = None,
    web_access_enabled: bool = False,
    git_access_enabled: bool = False,
    git_available: bool = True,
    skills_enabled: bool = False,
    git_hint: str | None = None,
) -> dict[str, CapabilityStatus]:
    """默认按"git 关、技能关"算一遍能力状态，用例按需覆盖。

    构造开关表的方式与 `effective_capabilities` 同一口径。``web_access_enabled`` 这个参数**已经不再
    决定联网**（2026-10-14 起它被 `resolve_switches` 强制置真）——留着它，正是为了有用例能钉住
    "传 false 也照样联网"这条新口径。
    """
    layer = dict(as_switch_layer(PROFILES["coder"] if capabilities is None else capabilities))
    layer.update(
        {
            "web": web_access_enabled,
            "git_read": git_access_enabled,
            "git_write": git_access_enabled,
            "skill": skills_enabled,
        }
    )
    rows = capability_status(
        resolve_switches(layer), git_available=git_available, git_hint=git_hint
    )
    return {row.capability: row for row in rows}


def test_capability_status_covers_the_whole_closed_set() -> None:
    """闭集里**每个**能力都要有一行（含不可用的），否则页面会漏掉"为什么用不了"。"""
    rows = _status_rows(git_access_enabled=True)

    assert set(rows) == set(CAPABILITY_TOOLS)


def test_capability_status_separates_the_three_reasons() -> None:
    """三种原因各走一条路：可用 / 开关关闭 / 环境缺依赖（§16.1、§16.6）。

    2026-10-13：档位退出运行期绑定，原先的第四种原因（"档位没声明"）随之删除——页面不再有换档这个
    动作，留着它只会渲染出一句做不到的建议。现在开关关着与档位没声明合成同一条"开关关闭"。
    """
    # 默认（git 关）：只有 git 那一类落在"开关关闭"上
    rows = _status_rows()

    assert rows["plan"].reason == REASON_AVAILABLE
    # 关着的项一律指向**开关本身**，且必须说清"不用重启"
    assert rows["git_write"].reason == REASON_DISABLED
    assert "不用重启" in (rows["git_write"].hint or "")

    # 联网自 2026-10-14 起是**常态能力**：即使调用方传 `web_access_enabled=False`，它照样可用且锁定；
    # 页面理由讲的是"为什么关不掉"，而不是"去面板里打开它"。
    assert rows["web"].reason == REASON_AVAILABLE and rows["web"].locked is True
    assert rows["web"].hint is None
    assert "始终开启" in rows["web"].detail

    # 开关打开且环境就绪：git 转为可用（这条与上一条互为对照，避免"永远返回不可用"也能过）
    ready = _status_rows(git_access_enabled=True)
    assert ready["git_read"].available is True and ready["git_read"].hint is None

    # 环境缺依赖：开关开着，但这台机器没有 git —— 提示用**传进来的**那句话（"缺什么、怎么装"）
    missing = _status_rows(git_access_enabled=True, git_available=False, git_hint="装好 Git 再重启")
    assert missing["git_read"].reason == REASON_MISSING_DEPENDENCY
    assert missing["git_read"].hint == "装好 Git 再重启"


def test_capability_status_never_leaks_tool_names() -> None:
    """页面能力区块**不展示原始工具名**（§16.6、§15.8 的字段门槛）。

    机械校验：能力名、中文名、说明、提示四类文本里都不能出现任何已实现工具的名字。
    """
    rows = _status_rows(git_access_enabled=True, git_available=False)
    text = " ".join(
        part
        for row in rows.values()
        for part in (row.capability, row.label, row.detail, row.hint or "")
    )

    for name in {name for names in CAPABILITY_TOOLS.values() for name in names}:
        assert name not in text, f"能力状态的对外文本里出现了工具名 {name}"

    # 每个能力都得有中文名（缺失会退化成能力 id，等于把内部标识怼到页面上）
    assert set(CAPABILITY_LABELS) == set(CAPABILITY_TOOLS)


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
        "search_files",
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
    """``RuntimeContext`` 的默认能力集就是现行 coder 工具集：既有构造点不传也不变。

    2026-10-13：档位退出运行期绑定，``agent_profile`` 字段随之删除，换成 ``agent_switches``
    （开关表，页面要如实回显"哪一项开着"）。默认能力集仍是 ``coder`` 那套——行为不变。
    """
    defaults = {field.name: field.default for field in fields(RuntimeContext)}

    assert defaults["agent_capabilities"] == PROFILES[DEFAULT_PROFILE]
    assert MODE_CAPABILITIES["tool"] == PROFILES[DEFAULT_PROFILE]
    # 开关表默认空：运行期由 `build_runtime_context` 用 config 的出厂默认补齐，不在这里写死一份。
    # 它用 `default_factory`（可变默认值不能共享），所以不能从 `field.default` 读。
    switches_field = next(field for field in fields(RuntimeContext) if field.name == "agent_switches")
    assert switches_field.default_factory() == {}


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
