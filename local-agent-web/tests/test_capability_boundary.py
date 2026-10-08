"""能力边界：不可见的工具既不能调用、也不能执行（架构 §6.9、§16.1；T51）。

三条边界各有一组用例：

1. **提示**：本档看不到的工具名不得出现在系统提示里——提示只能承诺做得到的事；
2. **全能力档不变**：默认档两段提示的指纹被固定，改动它等于改动稳定前缀（供应商缓存会重算）；
3. **执行**：模型即使报出不可见工具的名字，也必须被拒绝且**没有副作用**。

第 3 条来自真实实证：2026-10-08 用 ``reviewer``（只读）档跑固定场景时，它照着提示词里
"新建文件用 write_file"那句发起了 ``write_file`` 调用，而当时的执行节点按**全量**注册表把它跑掉了
——工作区里真的多出一个文件。所以"没绑定给模型"必须同时是"不能执行"。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from local_agent.capabilities import CAPABILITY_TOOLS, DEFAULT_PROFILE, PROFILES, is_visible
from local_agent.config import AppConfig, ProfilesConfig
from local_agent.context import prompt_context
from local_agent.graph import builder
from local_agent.graph.state import RuntimeContext
from local_agent.llm.usage_recording import fingerprint_text

ModelFactory = Callable[..., Any]

#: 已登记工具名的全集：扫描提示词时的"不该出现的名字"。
ALL_TOOL_NAMES: tuple[str, ...] = tuple(
    name for names in CAPABILITY_TOOLS.values() for name in names
)

#: 全能力档（默认 ``coder``）两段提示的指纹。
#:
#: **这个值变了就说明稳定系统前缀变了**：供应商缓存会整段重算（同口径见
#: `memory.compression.tool_step_budget_chars` 记的 19.9% 命中率那次）。改措辞前先想清楚代价，
#: 并同步 `docs/planning/FUTURE_WORK.md` 的缓存口径。
FULL_PROFILE_PROMPT_FINGERPRINT = "2ed5e3767351e83b"

#: 只读档应当明确说出的限制（提示里缺了它，模型就会照着自己的猜测硬编）。
READ_ONLY_LIMIT_PHRASE = "本次运行的能力有上限"


def _config_for(app_config: AppConfig, profile: str) -> AppConfig:
    """同一份基准配置换一个 Profile（只改这一项，其余按 conftest 的通用配置）。"""
    return app_config.model_copy(update={"profiles": ProfilesConfig(active=profile)})


def _context(tmp_path: Path, model: object, app_config: AppConfig) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        current_cwd=tmp_path,
    )


def _write_call(path: Path) -> list[dict[str, Any]]:
    return [{"name": "write_file", "args": {"path": path.name, "content": "复制的内容"}, "id": "call_1"}]


# --------------------------------------------------------------------------- 绑定与可见性


def test_visibility_matrix() -> None:
    """可见性：chat 只有只读子集；只读档没有写类与 Shell；未知能力名抛错而非静默空集。"""
    assert is_visible("chat", PROFILES[DEFAULT_PROFILE], "read_file")
    assert not is_visible("chat", PROFILES[DEFAULT_PROFILE], "write_file")
    assert is_visible("tool", PROFILES[DEFAULT_PROFILE], "run_shell")
    assert not is_visible("tool", PROFILES["researcher"], "edit_file")
    assert not is_visible("tool", PROFILES["reviewer"], "update_plan")

    with pytest.raises(KeyError):
        is_visible("tool", ("未登记的能力",), "read_file")


def test_full_capability_profile_prompt_sections_are_frozen() -> None:
    """默认档：两段提示逐字不变（指纹固定），且不出现"能力有上限"这类只读提示。"""
    text = prompt_context.render_capability_rules(
        PROFILES[DEFAULT_PROFILE]
    ) + prompt_context.render_tool_usage_rules(PROFILES[DEFAULT_PROFILE])

    assert fingerprint_text(text) == FULL_PROFILE_PROMPT_FINGERPRINT
    assert READ_ONLY_LIMIT_PHRASE not in text


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_prompt_never_names_a_tool_the_profile_cannot_see(
    profile: str, tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """系统提示里出现工具名，就必须是本档可见的工具——两个模式都查。"""
    context = _context(tmp_path, model_factory(decision="tool", reply="好"), _config_for(app_config, profile))
    hidden: list[str] = []

    for mode in ("chat", "tool"):
        prompt = prompt_context.build_stable_system_prompt(
            mode=mode, context=context
        ).system_prompt
        hidden.extend(
            f"{mode}:{name}"
            for name in ALL_TOOL_NAMES
            if name in prompt and not is_visible(mode, PROFILES[profile], name)
        )

    assert hidden == []


@pytest.mark.parametrize("profile", ["researcher", "reviewer"])
def test_read_only_profiles_state_the_missing_capability(profile: str) -> None:
    """只读档必须把"做不到什么"写进提示，并给出可操作建议（切档重试）。"""
    text = prompt_context.render_capability_rules(
        PROFILES[profile]
    ) + prompt_context.render_tool_usage_rules(PROFILES[profile])

    assert READ_ONLY_LIMIT_PHRASE in text
    assert "修改、新建或删除文件" in text
    assert "执行命令" in text
    assert "档位后重试" in text
    # 写类工具的用法说明在本档必须消失（它就是那次误调用的来源）
    assert "write_file" not in text
    assert "edit_file" not in text


# --------------------------------------------------------------------------- 执行边界


def test_invisible_tool_call_is_denied_without_side_effect(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """只读档：模型报出 ``write_file`` → 被拒、不进审批、磁盘不变。"""
    target = tmp_path / "notes_copy.md"
    model = model_factory(
        decision="tool",
        reply="当前配置不能写文件。",
        tool_calls=_write_call(target),
        reflection={"goal_achieved": True, "next_action": "finish"},
    )
    context = _context(tmp_path, model, _config_for(app_config, "reviewer"))

    result = builder.build_chat_graph().invoke(
        {"user_input": "把 notes.md 复制一份为 notes_copy.md"}, context=context
    )

    assert not target.exists(), "只读档不得产生写入副作用"
    assert result["tool_results"][0]["status"] == "denied"
    assert result.get("pending_approval") is None, "不可见的工具不该进入审批流程"
    reasons = [
        str(message.content) for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert any("能力范围" in reason for reason in reasons)


def test_the_same_call_succeeds_under_the_full_profile(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """对照组：同一个调用在默认档**会**真的执行——证明上面那条拒绝来自能力边界，而不是别的原因。"""
    target = tmp_path / "notes_copy.md"
    model = model_factory(
        decision="tool",
        reply="已复制。",
        tool_calls=_write_call(target),
        reflection={"goal_achieved": True, "next_action": "finish"},
    )
    context = _context(tmp_path, model, _config_for(app_config, DEFAULT_PROFILE))

    result = builder.build_chat_graph().invoke(
        {"user_input": "把 notes.md 复制一份为 notes_copy.md"}, context=context
    )

    assert target.read_text(encoding="utf-8") == "复制的内容"
    assert result["tool_results"][0]["status"] == "ok"
