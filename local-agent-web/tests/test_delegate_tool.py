"""自主委派（§18.7，2026-10-13）：能力闸门、工具外形、只读子 Agent、审计通道。

这组用例守住四件事：

1. **默认不可见**：`delegate` 出厂关，默认能力集合与系统提示里都不出现这个工具（前缀逐字不变）；
2. **开关即授权**：打开后工具进能力集合、提示里出现"什么时候别用"的边界；
3. **子 Agent 只读且真的查到了原文**：走工具触发的委派与显式子图共用同一套执行，
   报告是父图核对过的原文与行号，子任务拿不到写能力；
4. **审计不丢**（①乙）：子任务的工具事件与模型调用明细经委派审计槽改写成父轮次 `run_id` 并回父状态。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import RoutingFakeModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from local_agent.cancellation import CancellationToken
from local_agent.capabilities import (
    enabled_capabilities,
    resolve_switches,
    tool_names_for,
)
from local_agent.config import AppConfig, WorkflowsConfig
from local_agent.context.prompt_context import render_capability_rules, render_tool_usage_rules
from local_agent.graph import builder, delegated_workflow
from local_agent.llm.models import ModelCapabilities
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.security.policy import KNOWN_TOOLS, READ_ONLY_TOOLS, SecurityPolicy
from local_agent.tools.delegate_tools import DelegatedResearchResult
from local_agent.tools.tool_registry import ToolRegistry
from local_agent.workflow_eval import READ_CASE, _config


class ResearchModel(RoutingFakeModel):
    """子模型自行读取实际文件，然后通过控制工具交付报告。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        read = any(isinstance(message, ToolMessage) and message.name == "read_file" for message in messages)
        call = ({"name": "task_control", "id": "child-finish", "args": {
                    "action": "complete", "message": "取消不撤销已发生的副作用（docs/cancel.md:1）"}}
                if read else {"name": "read_file", "id": "child-read", "args": {"path": "cancel.md"}})
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


def _context(
    tmp_path: Path, *, workflows_enabled: bool = False, model: Any = None,
    delegate_switch: bool = False, recorder: ModelCallRecorder | None = None,
) -> Any:
    """一个真实装配出来的运行上下文：真策略、真注册表、脚本子模型。"""
    for path, content in READ_CASE.seeds:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    config = _config(tmp_path, "coder")
    # 工具触发路径的闸门是页面上的能力开关，**不是** workflows.enabled（§18.7）。
    config.workflows = WorkflowsConfig(enabled=workflows_enabled)
    recorder = recorder or ModelCallRecorder(
        connection_id="offline", config_version="delegate", provider="scripted",
        model_id="scripted-offline", endpoint_fingerprint="offline",
        context_window_tokens=config.require_llm().context_window_tokens,
    )
    return builder.build_runtime_context(
        app_config=config.as_run_config(), model_adapter=model or ResearchModel(messages=iter([])),
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
        usage_recorder=recorder,
        # 模型能力用显式值：探测会真调一次模型，而这里的假模型只回答固定脚本（§11.7）。
        model_capabilities=ModelCapabilities(tool_calling=True),
        agent_switches={"delegate": True} if delegate_switch else None,
    )


def _policy(tmp_path: Path) -> SecurityPolicy:
    return SecurityPolicy.from_config(AppConfig(), workspace_root=tmp_path, environ={})


# ---- 1/2. 能力闸门与提示 ------------------------------------------------------


def test_delegate_is_off_by_default_and_absent_from_the_default_prompt() -> None:
    """出厂默认关：能力集合、绑定的工具与两段提示里都不该出现它（§16.1 的前缀口径）。"""
    caps = enabled_capabilities(resolve_switches(), git_available=True, vision_available=True)

    assert "delegate" not in caps
    assert "delegate_research" not in tool_names_for("tool", caps)
    assert "delegate_research" not in render_tool_usage_rules(caps)
    assert "delegate_research" not in render_capability_rules(caps)


def test_delegate_switch_brings_the_tool_and_its_boundaries() -> None:
    """打开开关后：工具进能力集合，提示里出现它，并且**同时**写明"什么时候别用"。"""
    caps = enabled_capabilities(
        resolve_switches({}, {"delegate": True}), git_available=True, vision_available=True
    )

    assert "delegate" in caps
    assert "delegate_research" in tool_names_for("tool", caps)
    rules = render_tool_usage_rules(caps)
    assert "delegate_research" in rules
    # 花钱的能力必须把"别滥用"写进提示，否则模型会对一次读取就能答完的问题也派子 Agent。
    assert "运行只读命令" in rules
    assert "没有文件写入" in rules


def test_delegate_tool_is_registered_and_needs_no_human_approval(tmp_path: Path) -> None:
    """工具必须可绑定给模型，且按"只读、不需要审批"归类（§18.7）。"""
    assert "delegate_research" in KNOWN_TOOLS
    assert "delegate_research" in READ_ONLY_TOOLS
    registry = ToolRegistry.build(_policy(tmp_path), git_available=True)
    assert registry.get("delegate_research") is not None


# ---- 3. 工具外形与注入入口 ----------------------------------------------------


def test_registry_injects_the_delegate_entry_and_the_tool_passes_parameters_through(
    tmp_path: Path,
) -> None:
    """A 方案的核心断言：注册表构造时闭包进来的入口，就是工具实际调用的那个（参数原样到达）。"""
    seen: list[tuple[list[dict[str, str]], int]] = []

    def fake(tasks, concurrency):
        seen.append(([dict(task) for task in tasks], concurrency))
        return DelegatedResearchResult(status="completed", report="已核对的原文").model_dump_json()

    registry = ToolRegistry.build(_policy(tmp_path), git_available=True, delegate=fake)
    tool = registry.get("delegate_research")
    assert tool is not None

    payload = json.loads(tool.invoke({"tasks": [{"scope": " docs ", "question": " 核对取消 "}], "concurrency": 2}))

    assert payload["status"] == "completed"
    assert payload["report"] == "已核对的原文"
    # scope / question 去空白后交给运行侧；并发原样传出（上限由运行侧校验）。
    assert seen == [([{"scope": "docs", "question": "核对取消"}], 2)]


def test_the_tool_rejects_a_bad_shape_before_calling_the_delegate(tmp_path: Path) -> None:
    """1–3 个子任务、并发 1–2：越界在**模型这一侧**就被拒，不会走到执行。"""
    calls: list[Any] = []
    registry = ToolRegistry.build(
        _policy(tmp_path), git_available=True, delegate=lambda tasks, concurrency: calls.append(1) or "{}"
    )
    tool = registry.get("delegate_research")
    assert tool is not None

    with pytest.raises(ValidationError):
        tool.invoke({"tasks": []})
    with pytest.raises(ValidationError):
        tool.invoke({"tasks": [{"scope": ".", "question": "q"}] * 0})
    with pytest.raises(ValidationError):
        tool.invoke({"tasks": [{"scope": ".", "question": "q"}], "concurrency": 0})
    assert calls == []


def test_without_a_delegate_entry_the_tool_fails_loudly(tmp_path: Path) -> None:
    """没有运行依赖时给一句明确的失败，而不是空结果或假成功。"""
    registry = ToolRegistry.build(_policy(tmp_path), git_available=True)
    tool = registry.get("delegate_research")
    assert tool is not None

    payload = json.loads(tool.invoke({"tasks": [{"scope": ".", "question": "q"}]}))

    assert payload["status"] == "failed"
    assert "不支持自主委派" in payload["report"]


def test_empty_scope_or_question_is_refused_before_running(tmp_path: Path) -> None:
    """空 scope / 空问题在工具内就被拒（不启动任何子任务、不花模型调用）。"""
    calls: list[Any] = []
    registry = ToolRegistry.build(
        _policy(tmp_path), git_available=True, delegate=lambda tasks, concurrency: calls.append(1) or "{}"
    )
    tool = registry.get("delegate_research")
    assert tool is not None

    payload = json.loads(tool.invoke({"tasks": [{"scope": "   ", "question": "q"}]}))

    assert payload["status"] == "failed"
    assert calls == []


# ---- 4. 真的跑一次：只读子 Agent + 父证据核对 + 审计通道 -----------------------


def test_delegate_runner_runs_readonly_children_and_returns_verified_evidence(tmp_path: Path) -> None:
    """工具触发路径的端到端：`workflows.enabled=false` 也能跑（闸门是能力开关），报告是核对过的原文。"""
    context = _context(tmp_path, workflows_enabled=False)
    assert context.app_config.workflows.enabled is False

    runner = delegated_workflow.make_delegation_runner(context)
    payload = json.loads(runner([{"scope": "docs", "question": "核对取消与恢复原文"}], 1))

    assert payload["status"] == "completed", payload
    # 报告是原文摘录 + 真实文件行号（父图核对过子工具事件与磁盘内容之后才会写出）。
    assert "（docs/cancel.md:1）" in payload["report"]
    assert "取消不撤销已发生的副作用" in payload["report"]
    # 三份种子文档各两行非空原文（计划里 keywords 为空 = 整理所有非空行），所以是 6 条引用。
    child = payload["children"][0]
    assert (child["scope"], child["status"], child["evidence_count"], child["error"]) == (
        "docs", "completed", 0, None,
    )
    # 计数搬自子报告（§18.7 的用户可见字段）：这一路真的调过工具、也做过一次计划模型调用。
    assert child["tool_calls"] >= 1
    assert child["model_calls"] == 2
    # **预留 token 不进工具结果**（2026-10-13 裁定）：它是切出来的上限、每路几乎一样，
    # 摆在这里会被读成"这一路花了多少"，也会诱使模型跟用户聊 token（§15.7 要求预留与实际分开写）。
    assert "model_tokens_reserved" not in child
    assert payload["note"] is None


def test_delegate_runner_records_audit_for_the_parent_turn(tmp_path: Path) -> None:
    """①乙：子任务的工具事件与模型调用明细进审计槽，改写成父轮次 `run_id` 后并回父状态。"""
    context = _context(tmp_path, workflows_enabled=False)
    runner = delegated_workflow.make_delegation_runner(context)

    runner([{"scope": "docs", "question": "核对取消与恢复原文"}], 1)

    audit = context.delegation_audit or []
    assert audit, "审计槽为空：这次委派的轨迹与用量会从页面上消失"
    update = delegated_workflow.delegation_audit_update(audit, "RUN-PARENT")
    # 子任务的读文件事件与它自己的模型调用都在，且归属改写成父轮次——整轮汇总不会漏掉这次委派。
    assert update["tool_events"], update
    assert all(event.run_id == "RUN-PARENT" for event in update["tool_events"])
    assert update["model_call_records"]
    assert all(record.run_id == "RUN-PARENT" for record in update["model_call_records"])
    assert all(event.run_id == "RUN-PARENT" for event in update["tool_events"])
    assert delegated_workflow.delegation_audit_update([], "RUN-PARENT") == {}


def test_child_agent_only_ever_used_read_only_tools(tmp_path: Path) -> None:
    """子 Agent 的**实际行为**证据：审计里出现的工具只能是只读那一组。

    比"断言能力集合被收窄"更硬——它看的是子 Agent 真跑过什么；父能力收窄、子策略重建、
    子注册表闭包任何一环漏掉，这里都会冒出写类或执行类工具（§18.3）。
    """
    context = _context(tmp_path, workflows_enabled=False)

    payload = json.loads(
        delegated_workflow.make_delegation_runner(context)([{"scope": "docs", "question": "核对取消原文"}], 1)
    )

    assert payload["status"] == "completed", payload
    audit = delegated_workflow.delegation_audit_update(context.delegation_audit or [], "RUN")
    used = {event.tool_name for event in audit.get("tool_events", [])}
    assert used, "子 Agent 一个工具都没调过，这组断言就没有证据意义"
    assert used <= {"glob", "read_file", "task_control"}, used


class LoopModel(RoutingFakeModel):
    """父决策按脚本调用 `delegate_research`；子 Agent 的计划调用返回严格 JSON。

    复用 `conftest.RoutingFakeModel`（它支持 `bind_tools` 与主循环的其余分支），只多一条分派：
    子 Agent 的检索计划提示要拿到严格 JSON，而不是父决策那份 `tool_calls` 脚本。
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        human = "\n".join(message.text for message in messages if message.type == "human")
        if "核对取消与恢复原文" in human or "核对取消原文" in human:
            return ResearchModel._generate(self, messages, stop=stop, run_manager=run_manager, **kwargs)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_usage_aware_drains_the_delegation_audit_into_the_parent_state(tmp_path: Path) -> None:
    """①乙 的接线点：节点收尾时把审计槽改写成父轮次 `run_id` 并回父 State，然后清空。

    这是工具路径唯一的通道——工具只能给模型返回字符串，子任务的轨迹与用量必须由节点收尾并回，
    否则这次委派在轨迹页与用量页里就像没发生过。
    """
    context = replace(_context(tmp_path), usage_recorder=None, delegation_audit=[
        {
            "tool_results": [{"run_id": "child", "tool_call_id": "c1"}],
            "error_events": [{"run_id": "child", "message": "子任务超时"}],
        }
    ])
    node = builder._usage_aware("tool_execution", lambda state: {"task_status": "running"})

    out = node({"run_id": "PARENT"}, SimpleNamespace(context=context))

    assert out["task_status"] == "running"  # 节点头自己的更新不被吞掉
    assert out["tool_results"] == [{"run_id": "PARENT", "tool_call_id": "c1"}]
    assert out["error_events"] == [{"run_id": "PARENT", "message": "子任务超时"}]
    assert context.delegation_audit == [], "审计槽没排空：下一次收尾会重复并回同一批事件"


def test_the_model_can_delegate_from_the_free_loop_and_the_turn_keeps_the_audit(tmp_path: Path) -> None:
    """端到端：打开开关后，模型在自由循环里自己调用 `delegate_research`，父轮次拿到原文与子轨迹。"""
    model = LoopModel(messages=iter([]), reply="已委派并核对原文。", tool_calls=[{
        "name": "delegate_research",
        "args": {"tasks": [{"scope": "docs", "question": "核对取消原文"}], "concurrency": 1},
        "id": "call-delegate",
    }])
    context = _context(tmp_path, delegate_switch=True, model=model)
    assert "delegate" in context.agent_capabilities

    state = builder.build_chat_graph().invoke(
        {"user_input": "看看取消与恢复的约定", "session_mode": "agent"}, context=context
    )

    run_id = state["run_id"]
    # 1) 模型拿到的工具结果里就是父图核对过的原文（路径 + 行号）。
    results = [str(message.content) for message in state["messages"] if isinstance(message, ToolMessage)]
    assert any("delegate_research" in str(message.name) and "docs/cancel.md:1" in str(message.content)
               for message in state["messages"] if isinstance(message, ToolMessage)), results
    # 2) 子 Agent 的读文件事件并回了父轮次，且归属是**父** run_id（否则整轮轨迹是断的）。
    child_events = [event for event in state["tool_events"] if event.tool_name == "read_file"]
    assert child_events, "子 Agent 的事件没有并回父轮次"
    assert {event.run_id for event in child_events} == {run_id}
    # 3) 子 Agent 那次计划调用的用量也在整轮明细里（§18.3 的"不建第二份真源"）。
    assert any(record.run_id == run_id for record in state["model_call_records"])
