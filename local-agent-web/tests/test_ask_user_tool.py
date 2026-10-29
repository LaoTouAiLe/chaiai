"""`ask_user` 工具契约（架构 §6.14）：参数校验、能力归属、绑定顺序与免审批。

本文件只锁**工具本身**的契约。挂起与恢复（图、状态、接口、事件）由后续任务覆盖；
在那些落地之前，调用它会像普通工具一样返回一句 JSON——这里不把那个中间态当契约固定。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from local_agent.capabilities import (
    CAPABILITY_LABELS,
    CAPABILITY_TOOLS,
    LOCKED_CAPABILITIES,
)
from local_agent.config import SecurityConfig, WorkspaceConfig
from local_agent.security.policy import CONTROL_TOOLS, KNOWN_TOOLS, SecurityPolicy
from local_agent.tools.plan_tools import AskUser, build_plan_tools


def _tools() -> dict[str, object]:
    return {tool.name: tool for tool in build_plan_tools()}


# ---- 1. 正常路径 -------------------------------------------------------------


def test_ask_user_returns_the_question_and_trims_surrounding_space() -> None:
    # 单题写法归一成一题组（2026-10-28）：模型看到的是一份确定的题组结构。
    payload = json.loads(_tools()["ask_user"].invoke({"question": "  部署到哪个环境？  "}))
    assert payload == {
        "questions": [{"question": "部署到哪个环境？", "options": [], "allow_free_text": True}]
    }


def test_ask_user_accepts_a_group_with_options() -> None:
    payload = json.loads(
        _tools()["ask_user"].invoke({
            "questions": [
                {"question": "部署到哪个环境？", "options": ["测试环境", "生产环境"]},
                {"question": "要不要先备份？", "options": ["要", "不要"], "allow_free_text": False},
            ]
        })
    )
    assert [item["options"] for item in payload["questions"]] == [
        ["测试环境", "生产环境"],
        ["要", "不要"],
    ]
    assert payload["questions"][1]["allow_free_text"] is False


@pytest.mark.parametrize(
    "questions",
    [
        [{"question": "问题", "options": ["a", "b", "c", "d"]}],  # 每题最多 3 个选项
        [{"question": f"问题{i}"} for i in range(6)],  # 一次最多 5 题
        [{"question": "   "}],  # 空问题
    ],
    ids=["too-many-options", "too-many-questions", "blank-question"],
)
def test_ask_user_rejects_out_of_range_groups(questions: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        _tools()["ask_user"].invoke({"questions": questions})


def test_ask_user_schema_forbids_extra_arguments() -> None:
    """参数是闭集：不许带 tool_call_id 这类字段；选项必须写在每题的 `options` 里。"""
    with pytest.raises(ValidationError):
        _tools()["ask_user"].invoke({"question": "哪个分支？", "options": ["main"]})
    assert set(AskUser.model_fields) == {"question", "questions"}


# ---- 2. 错误路径：不合法的提问不产生任何挂起 ---------------------------------


@pytest.mark.parametrize(
    "question",
    ["", "   ", "长" * 501, "问\x00题", "问\x07题"],
    ids=["empty", "blank", "too-long", "nul-char", "bell-char"],
)
def test_invalid_questions_are_rejected(question: str) -> None:
    with pytest.raises(ValidationError):
        _tools()["ask_user"].invoke({"question": question})


def test_newline_and_tab_are_allowed() -> None:
    """换行与制表符是正常文本，不跟控制字符一起拒掉（问题常常需要分两行）。"""
    payload = json.loads(_tools()["ask_user"].invoke({"question": "第一行\n第二行\t结束"}))
    assert payload["questions"][0]["question"] == "第一行\n第二行\t结束"


# ---- 3. 安全边界：能力归属、绑定顺序、免审批 ---------------------------------


def test_ask_user_belongs_to_task_lifecycle_after_task_control() -> None:
    """归 `task_lifecycle`，且排 `task_control` 之后（追加即不重排既有绑定顺序，§6.9）。"""
    names = CAPABILITY_TOOLS["task_lifecycle"]
    assert "ask_user" in names
    assert names.index("ask_user") > names.index("task_control")
    # 该能力不可关闭：提问是"能不能用助手"的一部分，不该另给一个开关造出"能结束但不能问"。
    assert "task_lifecycle" in LOCKED_CAPABILITIES
    assert CAPABILITY_LABELS["task_lifecycle"]


def test_every_plan_tool_is_registered_exactly_once() -> None:
    registered = [name for names in CAPABILITY_TOOLS.values() for name in names]
    names = [tool.name for tool in build_plan_tools()]
    assert names == ["update_plan", "task_control", "ask_user"]
    for name in names:
        assert registered.count(name) == 1, f"{name} 必须且只能归属一个能力"


def test_ask_user_is_known_and_needs_no_approval(tmp_path: Path) -> None:
    assert "ask_user" in KNOWN_TOOLS and "ask_user" in CONTROL_TOOLS
    policy = SecurityPolicy(
        workspace_root=tmp_path, security=SecurityConfig(), workspace=WorkspaceConfig()
    )
    decision = policy.classify_tool("ask_user", {"question": "哪个分支？"})
    assert decision.allowed is True
    assert decision.requires_approval is False
    # 既有两个控制工具的审计文案逐字不变（避免既有 ToolEvent 文案漂移）
    assert policy.classify_tool("task_control", {"action": "continue"}).reason == "当前任务计划更新"


# ---- 4. 与 task_control 的分工必须写进说明里（否则模型分不清） ----------------


def test_tool_descriptions_point_at_each_other() -> None:
    tools = _tools()
    assert "ask_user" in tools["task_control"].description
    assert "task_control" in tools["ask_user"].description
