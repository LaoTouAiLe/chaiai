"""T04 验收：AGENTS 规则加载与模式路由（架构 §5、§7）。

覆盖扫描边界（Git 根、工作区上界）、多层与 override 优先级、截断、以及
``mode_router`` 的 LLM 分类与规则兜底。全部使用临时目录与假模型，不联网。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from local_agent import scope
from local_agent.application import errors as app_errors
from local_agent.application import projection as app_projection
from local_agent.config import AgentsConfig, AppConfig
from local_agent.context import agents_loader, prompt_context
from local_agent.exceptions import MemoryStoreError
from local_agent.graph import builder, nodes, projection, routers
from local_agent.graph.routers import ROUTE_CLASSIFIER_MARKER, RouteDecision
from local_agent.reflection.reflector import ReflectionDecision


def _assert_usage_adds_up(result: dict[str, Any]) -> None:
    """``prompt_tokens`` 必须等于各分项之和（``token_budget`` 声明的不变量）。"""
    usage = result["token_usage"]
    assert (
        usage.system_prompt_tokens
        + usage.window_messages_tokens
        + usage.tool_definition_tokens
    ) == usage.prompt_tokens
from local_agent.graph.state import RAGReference, RuntimeContext, ToolEvent
from local_agent.llm.models import (
    TOOL_CALLING_UNAVAILABLE_REPLY,
    ModelCapabilities,
    probe_capabilities,
)
from local_agent.llm.protocol import StructuredOutputError, call_structured
from local_agent.memory import long_term, medium_term
from local_agent.memory.medium_term import MediumTermHit
from local_agent.memory.schemas import MemoryReference
from local_agent.reflection import reflector

ModelFactory = Callable[..., object]


# --------------------------------------------------------------------------- 夹具


def _agents_config(**overrides: object) -> AgentsConfig:
    data: dict[str, object] = {
        "enabled": True,
        "filenames": ["AGENTS.md", "AGENTS.override.md"],
        "max_file_chars": 20000,
        "max_total_chars": 60000,
        "precedence": {"closer_directory_wins": True, "override_wins": True},
    }
    data.update(overrides)
    return AgentsConfig.model_validate(data)


def _config_with_agent(app_config: AppConfig, **overrides: object) -> AppConfig:
    data = app_config.model_dump()
    data["agent"].update(overrides)
    return AppConfig.model_validate(data)


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    """构造工作区根（含 ``.git``）与子目录。"""
    workspace = tmp_path / "ws"
    child = workspace / "pkg"
    child.mkdir(parents=True)
    (workspace / ".git").mkdir()
    return workspace, child


def _context(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    **model_overrides: object,
) -> RuntimeContext:
    return _context_with(tmp_path, model_factory(**model_overrides), app_config)


def _context_with(tmp_path: Path, model: object, app_config: AppConfig) -> RuntimeContext:
    return builder.build_runtime_context(
        app_config=app_config,
        model_adapter=model,  # type: ignore[arg-type]
        workspace_root=tmp_path,
        current_cwd=tmp_path,
    )


# --------------------------------------------------------------------------- AGENTS 加载


def test_merges_from_git_root_to_current_directory(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("根目录规则", encoding="utf-8")
    (child / "AGENTS.md").write_text("子目录规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=workspace, current_cwd=child
    )

    assert [path.name for path in loaded.files] == ["AGENTS.md", "AGENTS.md"]
    # 越近的目录优先级越高，排在后面
    assert loaded.text.index("根目录规则") < loaded.text.index("子目录规则")
    assert not loaded.truncated
    assert loaded.estimated_tokens > 0


def test_git_root_outside_workspace_is_not_read(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    (outer / ".git").mkdir()
    (outer / "AGENTS.md").write_text("工作区外规则", encoding="utf-8")
    (inner / "AGENTS.md").write_text("工作区内规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=inner, current_cwd=inner
    )

    assert "工作区外规则" not in loaded.text
    assert "工作区内规则" in loaded.text


def test_without_git_repo_stops_at_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    child = workspace / "pkg"
    child.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("工作区根规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=workspace, current_cwd=child
    )

    assert "工作区根规则" in loaded.text


def test_current_directory_outside_workspace_loads_nothing(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("工作区规则", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=workspace, current_cwd=outside
    )

    assert loaded.is_empty


def test_override_file_replaces_base_file(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("基础规则", encoding="utf-8")
    (workspace / "AGENTS.override.md").write_text("覆盖规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=workspace, current_cwd=child
    )

    assert [path.name for path in loaded.files] == ["AGENTS.override.md"]
    assert "覆盖规则" in loaded.text
    assert "基础规则" not in loaded.text


def test_override_wins_false_loads_both_files(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("基础规则", encoding="utf-8")
    (workspace / "AGENTS.override.md").write_text("覆盖规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(precedence={"closer_directory_wins": True, "override_wins": False}),
        workspace_root=workspace,
        current_cwd=child,
    )

    assert loaded.text.index("基础规则") < loaded.text.index("覆盖规则")


def test_closer_directory_wins_false_reverses_order(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("根目录规则", encoding="utf-8")
    (child / "AGENTS.md").write_text("子目录规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(precedence={"closer_directory_wins": False, "override_wins": True}),
        workspace_root=workspace,
        current_cwd=child,
    )

    assert loaded.text.index("子目录规则") < loaded.text.index("根目录规则")


def test_file_truncation_keeps_head_and_tail(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("H" * 50 + "M" * 200 + "T" * 50, encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(max_file_chars=60), workspace_root=workspace, current_cwd=child
    )

    assert loaded.truncated
    assert agents_loader.TRUNCATION_MARKER.split("{")[0] in loaded.text
    assert "H" * 30 in loaded.text
    assert "T" * 30 in loaded.text


def test_total_budget_stops_loading_further_files(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("A" * 200, encoding="utf-8")
    (child / "AGENTS.md").write_text("CHILD-RULES" * 20, encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(max_file_chars=200, max_total_chars=120),
        workspace_root=workspace,
        current_cwd=child,
    )

    assert loaded.truncated
    assert len(loaded.files) == 1
    assert "CHILD-RULES" not in loaded.text


def test_agents_disabled_returns_empty(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_text("根目录规则", encoding="utf-8")

    loaded = agents_loader.load_agents(
        _agents_config(enabled=False), workspace_root=workspace, current_cwd=child
    )

    assert loaded.is_empty
    assert loaded.files == ()


def test_missing_files_return_empty(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=workspace, current_cwd=child
    )

    assert loaded.text == ""
    assert loaded.truncated is False


def test_undecodable_file_is_reported(tmp_path: Path) -> None:
    workspace, child = _workspace(tmp_path)
    (workspace / "AGENTS.md").write_bytes(b"\xff\xfe\x00\x01")

    loaded = agents_loader.load_agents(
        _agents_config(), workspace_root=workspace, current_cwd=child
    )

    assert "无法读取" in loaded.text


# --------------------------------------------------------------------------- 模式路由


def test_model_classification_decides_mode(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(
        tmp_path, model_factory, app_config, decision="tool", decision_reason="需要读取文件"
    )

    result = routers.decide_mode(user_text="看看 README 里写了什么", context=context)

    assert result.mode == "tool"
    assert "LLM 分类" in result.reason
    assert "需要读取文件" in result.reason


def test_classification_failure_falls_back_to_rules(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config, fail_classification=True)

    result = routers.decide_mode(user_text="帮我读取 README.md", context=context)

    assert result.mode == "tool"
    assert "规则兜底" in result.reason
    assert "RuntimeError" in result.reason  # 只记录异常类型，不记录异常文本
    assert "分类调用失败" not in result.reason


def test_invalid_json_falls_back_to_rules(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config, invalid_classification=True)

    result = routers.decide_mode(user_text="运行一下 pytest", context=context)

    assert result.mode == "tool"
    assert "规则兜底" in result.reason


def test_no_signal_defaults_to_chat(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config, fail_classification=True)

    result = routers.decide_mode(user_text="讲个笑话", context=context)

    assert result.mode == "chat"
    assert "未命中工具意图" in result.reason


def test_empty_input_is_chat(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config)

    result = routers.decide_mode(user_text="   ", context=context)

    assert result.mode == "chat"


def test_route_by_mode_maps_state_to_branch() -> None:
    assert routers.route_by_mode({"mode": "tool"}) == routers.ROUTE_TOOL
    assert routers.route_by_mode({"mode": "chat"}) == routers.ROUTE_CHAT
    assert routers.route_by_mode({}) == routers.ROUTE_CHAT


# --------------------------------------------------------------------------- 图


def test_graph_nodes_match_plan() -> None:
    graph = builder.build_chat_graph()

    assert set(graph.get_graph().nodes) == {
        "__start__",
        "prepare_context",
        "load_agents",
        "recall_memory",
        "mode_router",
        "retrieve_code_node",
        "compression_node",
        "planner_node",
        "agent_decision_node",
        "safety_gate_node",
        "approval_interrupt_node",
        "tool_execution_node",
        "reflection_node",
        "completion_router",
        "memory_write_node",
        "task_memory_node",
        "chat_node",
        "final_response_node",
        "__end__",
    }


def test_chat_request_is_answered_by_chat_node(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    graph = builder.build_chat_graph()
    context = _context(tmp_path, model_factory, app_config, decision="chat", reply="你好，我在")

    result = graph.invoke({"user_input": "你好"}, context=context)

    assert result["mode"] == "chat"
    assert result["final_response"] == "你好，我在"
    assert not result.get("tool_events")


def test_fixed_chat_session_reads_a_file_without_write_tools(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    (tmp_path / "note.txt").write_text("已核实的内容", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="文件写着：已核实的内容",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "read_1"}],
    )
    result = builder.build_chat_graph().invoke(
        {"user_input": "note.txt 写了什么", "session_mode": "chat"},
        context=_context_with(tmp_path, model, app_config),
    )

    assert result["mode"] == "chat"
    assert model.bound_tools == ["list_dir", "read_file"]
    assert [event.tool_name for event in result["tool_events"]] == ["read_file"]
    assert result["tool_results"][0]["status"] == "ok"
    assert result["final_response"] == "文件写着：已核实的内容"


def test_fixed_chat_session_rejects_a_hallucinated_write_call(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    model = model_factory(
        reply="闲聊会话不能创建文件",
        tool_calls=[
            {"name": "write_file", "args": {"path": "blocked.txt", "content": "bad"}, "id": "write_1"}
        ],
    )
    result = builder.build_chat_graph().invoke(
        {"user_input": "创建文件", "session_mode": "chat"},
        context=_context_with(tmp_path, model, app_config),
    )

    assert not (tmp_path / "blocked.txt").exists()
    assert result["tool_results"][0]["status"] == "denied"
    assert result["final_response"] == "闲聊会话不能创建文件"


def test_tool_call_is_executed_and_verified(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """完整工具闭环：模型提出调用 → 安全门放行 → 执行 → 机械校验 → 模型据此回答。"""
    (tmp_path / "note.txt").write_text("文件内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="我读到的是：文件内容甲",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["mode"] == "tool"
    # T17：反思判完成后走 finish，最终回答由反射节点那次不绑定工具的合成调用给出，
    # 不再回到 planner 再取一次决策（因此只有 1 轮工具循环）
    assert result["loop_count"] == 2
    assert [event.status for event in result["tool_events"]] == ["ok"]
    assert result["tool_results"][0]["status"] == "ok"
    assert "文件内容甲" in result["tool_results"][0]["output_preview"]
    assert result["reflection_result"].result_trust == "verified"
    # T17 起完成度由 LLM 反思判定，但以机械证据为准：这里证据是 verified，允许判完成
    assert result["reflection_result"].goal_achieved is True
    assert [evidence.verified for evidence in result["final_evidence"]] == [True]
    # 工具结果确实回填给了模型
    assert any("文件内容甲" in prompt for prompt in model.seen_prompts)
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert result["final_response"] == "我读到的是：文件内容甲"
    assert result["task_status"] == "completed"


def test_unfinished_tool_task_asks_user_with_visible_question(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具被拒 + 反思自称完成 → 被机械压制为 ask_user，页面上必须看到追问而不是空回答。

    复审实测：不补可展示回答时，这条路径会得到 ``final_response=None`` + ``failed``。
    """
    model = model_factory(
        decision="tool",
        reply="不该走到这里",
        tool_calls=[{"name": "unknown_tool", "args": {}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "ask_user_reason": "请确认要执行哪个操作",
        },
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "随便做点什么"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["reflection_result"].goal_achieved is False
    assert result["reflection_result"].next_action == "ask_user"
    assert result["final_response"] and "请确认要执行哪个操作" in result["final_response"]
    # 本轮已结束、在等用户补充：既不是 completed，也不是"还在跑"。
    assert result["task_status"] == "blocked"  # 未完成：不冒充 completed，也不谎称还在跑


def test_denied_tool_is_reported_back_to_model(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """未接入/未放行的工具直接拒绝（删除这类动作走审批，见下一个用例）。"""
    model = model_factory(
        decision="tool",
        reply="好的，我无法执行该操作",
        tool_calls=[{"name": "unknown_tool", "args": {}, "id": "call_1"}],
        # 工具被拒 → 证据等级 failed：反思不得声称完成（架构 §5.4），继续想办法
        reflection={"goal_achieved": False, "next_action": "continue"},
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "随便做点什么"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["tool_results"][0]["status"] == "denied"
    assert result["reflection_result"].result_trust == "failed"
    assert result["final_evidence"][0].verified is False
    denial_messages = [
        message.text for message in result["messages"] if isinstance(message, ToolMessage)
    ]
    assert any("已拒绝执行" in text for text in denial_messages)
    assert result["final_response"] == "好的，我无法执行该操作"


def test_a_policy_denied_read_is_reported_as_blocked_not_failed(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工作区外读取被拒 → 报"被安全策略拦截"，而不是"工具执行失败"。

    只读工具在安全门一律放行，真正的边界拦截发生在**工具内部**。所以 `stage` 必须由**原因**决定
    而不是由捕获位置决定：报成"执行失败"会让用户去改工具参数，而他真正要改的是路径或请求本身
    （复审实证，2026-09-28：页面上显示的是"工具执行失败 TOOL_CALL_FAILED"）。
    """
    (tmp_path / "outside.txt").write_text("外面", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="读不到那个文件",
        tool_calls=[{"name": "read_file", "args": {"path": "../outside.txt"}, "id": "out-1"}],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读一下工作区外面的文件"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["tool_results"][0]["status"] == "denied"
    stages = [event["stage"] for event in result["error_events"]]
    assert "safety_gate" in stages  # 投影后是 TOOL_CALL_BLOCKED
    assert "tool_execution" not in stages


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    """取出挂起中的审批载荷。"""
    raw = result.get("__interrupt__")
    assert raw, "期望图暂停在人工审批"
    return dict(raw[0].value)


def test_loop_limit_blocks_continue_on_the_last_round(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """达到上限且反思仍想继续 → 停下来并明确说明（架构 §11.5）。"""
    config = _config_with_agent(app_config, max_tool_loops=1)
    model = model_factory(
        decision="tool",
        reply="不该走到这里",
        tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "call_1"}],
        reflection={"goal_achieved": False, "next_action": "continue"},
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "列出目录"}, context=_context_with(tmp_path, model, app_config=config)
    )

    assert result["loop_count"] == 1
    assert result["task_status"] == "failed"
    assert result["final_response"] == routers.LOOP_LIMIT_REPLY


def test_loop_limit_does_not_block_finish_on_the_last_round(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """上限轮已经拿到 verified + finish：正常终止不能被上限改成"失败"。

    复审实证：原先 ``completion_route`` 先判上限，会把 ``verified + finish`` 改成
    ``loop_limit + failed``，正常结论在边界轮次丢失。
    """
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    config = _config_with_agent(app_config, max_tool_loops=1)
    model = model_factory(
        decision="tool",
        reply="文件内容是甲",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={"goal_achieved": True, "next_action": "finish"},
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"}, context=_context_with(tmp_path, model, app_config=config)
    )

    assert result["loop_count"] == 1
    assert result["reflection_result"].result_trust == "verified"
    assert result["reflection_result"].next_action == "finish"
    assert result["final_response"] == "文件内容是甲"
    assert result["task_status"] == "completed"
    _assert_usage_adds_up(result)  # 正常合成路径


def test_loop_limit_does_not_block_ask_user_on_the_last_round(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """上限轮判定需要用户补充：追问优先于"循环上限失败"。"""
    config = _config_with_agent(app_config, max_tool_loops=1)
    model = model_factory(
        decision="tool",
        reply="不该走到这里",
        tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "call_1"}],
        reflection={
            "goal_achieved": False,
            "next_action": "ask_user",
            "ask_user_reason": "请确认要对哪个目录操作",
        },
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "列出目录"}, context=_context_with(tmp_path, model, app_config=config)
    )

    assert result["loop_count"] == 1
    assert result["reflection_result"].next_action == "ask_user"
    assert result["final_response"] and "请确认要对哪个目录操作" in result["final_response"]
    # 本轮已结束、在等用户补充：既不是 completed，也不是"还在跑"。
    assert result["task_status"] == "blocked"


def test_stall_guard_stops_the_loop_when_the_model_has_nothing_to_do(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """空转保护（实测）：计划还有步骤、模型连续只回"任务已完成" → 循环必须在两三轮内停下。

    修复前：反思判不出完成（只看到工具输出的元数据头部）→ 一直 continue，模型每轮都回
    "任务已完成 / 无新指令"，直到 ``max_tool_loops``（示例配置 12）被烧完才停下；用户最终看到的
    是一句自相矛盾的追问，而不是早已得出的结论。
    """
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="已完成，任务结束",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        plan_steps=["读取 note.txt", "总结内容"],
        reflection={"goal_achieved": False, "next_action": "continue"},
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt 并总结"},
        context=_context_with(tmp_path, model, app_config),
    )

    assert result["max_loop_count"] > 3  # 默认上限足够大，说明是保护生效而不是被上限拦下
    assert result["loop_count"] <= 3  # 不空转：读完文件后最多再两轮就收口
    assert result["reflection_result"].next_action == "ask_user"
    assert result["task_status"] != "completed"  # 计划没走完不冒充完成
    assert result["final_response"] and "连续 2 轮没有产生可执行的动作" in result["final_response"]
    # 被卡住的是**第二步**（「总结内容」——模型只回了一句客套话，没产出总结），不是第一步：
    # 第一步「读取 note.txt」的工具证据是 ``verified``，机械校验确认它确实做完了，所以标 ``completed``
    # （2026-09-30：多步计划的中间步骤不再每一步都问模型，完成度在证据通过校验时由机械结论给出，§5.4）。
    # 这条期望原先写的是 ``["blocked", "pending"]``——那是在"每一步的推进都必须等模型判 goal_achieved"
    # 的口径下写的，后果是把一个**已经成功**的步骤标成 ``blocked``（用户会以为读文件也失败了）。
    assert [step.status for step in result["plan"]] == ["completed", "blocked"]


def test_final_synthesis_stays_within_budget_with_large_tool_output(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具输出很大时，``finish`` 的收尾合成不能把整段窗口直接发出去。

    复审实证：合成调用发生在工具执行之后，却沿用压缩节点在工具执行**之前**算好的预算快照，
    也没算指令本身；大输出会让收尾请求超上下文，State 自报的预算也与真实出站 Prompt 不一致。
    """
    (tmp_path / "big.txt").write_text("甲" * 8000, encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="大文件读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "big.txt"}, "id": "call_1"}],
        reflection={"goal_achieved": True, "next_action": "finish"},
    )
    # 预算 = 窗口 − 输出预留。留够"有界证据 + 指令"能放下的空间，这条用例才验证的是"有界证据"
    # 这条路径（提示词变长会挤掉它）；连固定部分都放不下时改走确定性摘要，那条路径另有用例。
    # 方案 1 加长稳定前缀后，2K 窗口连固定部分都放不下（必定 PromptBudgetExceeded），
    # 因此把窗口抬到"前缀 + 有界证据 + 指令"够放、但整段工具输出仍放不下的区间。
    config = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"context_window_tokens": 6144, "max_tokens": 512}
            )
        }
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读大文件"}, context=_context_with(tmp_path, model, config)
    )

    assert result["final_response"] == "大文件读完了"
    # 收尾调用发的是"有界证据 + 指令"，不是整段工具输出
    assert reflector.TOOL_DATA_OPEN in model.seen_prompts[-1]
    assert "甲" * 3000 not in model.seen_prompts[-1]
    # State 自报的预算与这次调用对得上，且这次调用真的在预算内（固定上限也不一定放得下）
    budget = result["context_budget"]
    assert budget.window_messages == 1
    assert budget.estimated_prompt_tokens == result["token_usage"].prompt_tokens
    assert budget.estimated_prompt_tokens <= budget.prompt_budget_tokens
    _assert_usage_adds_up(result)  # 有界证据路径（指令仍算在系统提示分项里）


def test_final_synthesis_skips_the_model_when_the_prompt_does_not_fit(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """连"系统提示 + 指令"都放不下时不发请求：改用确定性摘要并记一条降级事件。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="文件内容是甲",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={"goal_achieved": True, "next_action": "finish"},
    )
    # 预算（1024 − 1000 = 24 token）远小于系统提示本身
    config = app_config.model_copy(
        update={
            "llm": app_config.llm.model_copy(
                update={"context_window_tokens": 1024, "max_tokens": 1000}
            )
        }
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读文件"}, context=_context_with(tmp_path, model, config)
    )

    assert result["final_response"]
    assert "PromptBudgetExceeded" in result["final_response"]
    assert any(
        event["stage"] == f"{nodes.REFLECTION_NODE}:final_answer"
        and "PromptBudgetExceeded" in str(event["message"])
        for event in result.get("error_events", [])
    )
    # 只有第一次决策调用（返回工具调用）打过模型：合成没有发出任何请求
    assert model.decision_calls == 1
    _assert_usage_adds_up(result)  # 不调用模型路径（窗口分项为 0）


class _StubMediumStore:
    """中期库替身：召回恒为空，写入收集到 ``records``（真实 Chroma 行为由 T14B 用例覆盖）。

    ``fail_write=True`` 时 ``upsert`` 抛 ``MemoryStoreError``，用来验证"单层失败不影响另一层"。
    """

    def __init__(self, *, fail_write: bool = False) -> None:
        self.records: list[Any] = []
        self.fail_write = fail_write

    def embed_query(self, text: str) -> list[float]:
        del text
        return [0.0]

    def query(self, **kwargs: Any) -> list[Any]:
        del kwargs
        return []

    def upsert(self, records: Sequence[Any]) -> int:
        if self.fail_write:
            raise MemoryStoreError("中期库写入失败（测试故意触发）")
        self.records.extend(records)
        return len(records)


def _memory_write_events(result: dict[str, Any]) -> list[Any]:
    """``memory_write_node`` 记下的错误事件。"""
    return [
        event
        for event in result.get("error_events", [])
        if event["stage"] == nodes.MEMORY_WRITE_NODE
    ]


def _config_with_long_term(app_config: AppConfig, path: Path, **overrides: object) -> AppConfig:
    """在示例配置上打开长期记忆，库文件指向临时目录。"""
    data = app_config.model_dump()
    data["memory"]["long_term"].update({"enabled": True, "path": str(path), **overrides})
    return AppConfig.model_validate(data)


def _long_term_fact_values(
    memory_path: Path, workspace: Path, *, name: str, entity_type: str = "project"
) -> list[str]:
    """读回某个实体下的当前事实值（用于断言"确实写进了长期库"）。"""
    opened = long_term.open_store(memory_path)
    try:
        entity = opened.find_entity(
            entity_type=entity_type,
            name=name,
            scope=scope.SCOPE_PROJECT,
            project_id=scope.project_id_for(workspace),
        )
        if entity is None:
            return []
        return [item.value_json for item in opened.list_facts(entity_id=entity.id)]
    finally:
        opened.close()


def test_finish_with_candidates_writes_long_term_memory(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """任务完成且反思给出候选时经 ``memory_write_node`` 落库（架构 §5.3、§10.4）。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                    "subject": "chaiai",
                    "importance": 0.7,
                    "confidence": 0.9,
                }
            ],
        },
    )
    memory_path = tmp_path / "data" / "memories.sqlite"

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(
            tmp_path, model, _config_with_long_term(app_config, memory_path)
        ),
    )

    assert result["task_status"] == "completed"
    assert len(result["memory_write_candidates"]) == 1
    assert _long_term_fact_values(memory_path, tmp_path, name="chaiai") == ['"内容甲"']


def test_finish_without_candidates_writes_no_memory(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """没有候选就不写：写入节点透传（库可能已被召回节点打开，但不能有任何记录）。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={"goal_achieved": True, "next_action": "finish"},
    )
    memory_path = tmp_path / "data" / "memories.sqlite"

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(
            tmp_path, model, _config_with_long_term(app_config, memory_path)
        ),
    )

    assert result["task_status"] == "completed"
    assert not result.get("memory_write_candidates")
    assert _long_term_fact_values(memory_path, tmp_path, name="chaiai") == []


def test_rejected_candidate_records_error_without_writing(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, legacy_loop: None
) -> None:
    """恢复的旧循环仍由写入层逐条拒绝非法候选；新提取的有限重试另测。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {"memory_type": "entity_fact", "content": "不是 JSON", "subject": "chaiai"}
            ],
        },
    )
    memory_path = tmp_path / "data" / "memories.sqlite"

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(
            tmp_path, model, _config_with_long_term(app_config, memory_path)
        ),
    )

    assert not result.get("memory_write_candidates")
    assert any(
        event["stage"] == nodes.MEMORY_WRITE_NODE and "被拒绝" in str(event["message"])
        for event in result.get("error_events", [])
    )
    assert _long_term_fact_values(memory_path, tmp_path, name="chaiai") == []


def test_chat_memory_writes_candidate_without_subject(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """候选缺 ``subject`` 也要写入成功（真实模型实证的回归）。

    这是用户实证里卡住跨会话召回的那一步：模型给出正确的 ``{"entity_type": "user", ...}``
    却漏了 ``subject``，候选被安全拒绝，于是"说了记住却没记住"。现在实体名由代码推导。
    """
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="chat",
        reply="好的",
        memory_requested=True,
        memory_intent=[
            {
                "memory_type": "entity_fact",
                "content": '{"entity_type": "user", "key": "test_runner", "value": "pytest"}',
                # 刻意不给 subject —— 这正是真实模型的输出形态
                "importance": 0.8,
                "confidence": 0.9,
            }
        ],
    )
    config = _config_with_long_term(app_config, memory_path)

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我个人习惯用 pytest"},
        context=_context_with(tmp_path, model, config),
    )

    assert result["memory_write_candidates"]  # 候选登记为完成
    assert "已保存到记忆：1 条" in str(result["final_response"])
    # 实体名推导为逻辑用户（未打开 allow_user_scope 时作用域仍是项目级）
    assert _long_term_fact_values(
        memory_path, tmp_path, name="local", entity_type="user"
    ) == ['"pytest"']
    assert not [
        event
        for event in result.get("error_events", [])
        if "被拒绝" in str(event["message"])
    ]


def _user_preference_intent() -> list[dict[str, Any]]:
    """一条"个人偏好"候选：``entity_type="user"``（是否真的写用户级还要看授权开关）。"""
    return [
        {
            "memory_type": "entity_fact",
            "content": '{"entity_type": "user", "key": "test_runner", "value": "pytest"}',
            "subject": "我",
            "importance": 0.8,
            "confidence": 0.9,
        }
    ]


def test_chat_memory_intent_writes_project_scope_by_default(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """闲聊里用户要求记住 → 落库；**默认不授权跨项目**，所以即使 entity_type=user 也落项目级。"""
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=_user_preference_intent(),
    )
    memory_path = tmp_path / "data" / "memories.sqlite"

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert "好的，我记住了" in str(result["final_response"])
    assert "已保存" in str(result["final_response"])  # 成功才确认
    assert result["task_status"] == "completed"  # 闲聊不会被审核结果标成"未完成"
    assert len(result["memory_write_candidates"]) == 1
    opened = long_term.open_store(memory_path)
    try:
        entity = opened.find_entity(
            entity_type="user",
            name="我",
            scope=scope.SCOPE_PROJECT,
            project_id=scope.project_id_for(tmp_path),
        )
        assert entity is not None  # 默认落项目级：模型说 user 也不会跨项目泄漏
        assert [item.value_json for item in opened.list_facts(entity_id=entity.id)] == ['"pytest"']
    finally:
        opened.close()


def test_chat_memory_intent_writes_user_scope_when_authorised(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """显式打开 ``memory.long_term.allow_user_scope`` 后，用户级事实才真的写进用户作用域。"""
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=_user_preference_intent(),
    )
    memory_path = tmp_path / "data" / "memories.sqlite"
    config = _config_with_long_term(app_config, memory_path, allow_user_scope=True)

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"},
        context=_context_with(tmp_path, model, config),
    )

    assert len(result["memory_write_candidates"]) == 1
    opened = long_term.open_store(memory_path)
    try:
        entity = opened.find_entity(
            entity_type="user",
            name="我",
            scope=scope.SCOPE_USER,
            user_id=scope.LOCAL_USER_ID,
        )
        assert entity is not None
        assert [item.value_json for item in opened.list_facts(entity_id=entity.id)] == ['"pytest"']
    finally:
        opened.close()


def test_plain_chat_does_not_write_memory(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """普通闲聊：审核过但空候选 → 不写、也不进写入节点（没有关键词兜底）。"""
    # 关掉原生 JSON Schema：审核调用才会经过假模型的 `_generate`，从而能在 `seen_prompts` 里
    # 观察到"确实审核过"（走结构化接口时那次调用不产生 Prompt 记录）。
    model = model_factory(
        decision="chat", reply="今天天气不错", support_json_schema=False
    )
    memory_path = tmp_path / "data" / "memories.sqlite"

    result = builder.build_chat_graph().invoke(
        {"user_input": "讲个笑话"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert result["task_status"] == "completed"
    assert not result.get("memory_write_candidates")
    assert not _memory_write_events(result)
    assert not any(
        record["stage"] == nodes.MEMORY_WRITE_NODE for record in result.get("decision_trace", [])
    )
    # 审核确实发生过，否则这条用例证明不了"空候选不写"
    assert any(reflector.MEMORY_INTENT_MARKER in prompt for prompt in model.seen_prompts)


def test_memory_intent_failure_records_error_and_keeps_answer(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """审核失败（记忆层开启时）：回答替换为确定性说明，事件里也记下原因。"""
    model = model_factory(decision="chat", reply="你好", fail_memory_intent=True)
    memory_path = tmp_path / "data" / "memories.sqlite"

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住我习惯用 pytest"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    # 不能只记事件：模型可能已经回答过"好的，我记住了"，最终回答必须把失败说出来
    assert "记忆意图审核失败" in str(result["final_response"])
    assert any(
        event["stage"] == f"{nodes.REFLECTION_NODE}:memory_intent"
        for event in result.get("error_events", [])
    )


def test_both_layers_disabled_still_audits_but_never_writes(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """两层都关闭时仍然审核（否则无法知道用户是否要求记住），但不会有任何写入、也不打扰闲聊。"""
    model = model_factory(decision="chat", reply="你好", support_json_schema=False)

    result = builder.build_chat_graph().invoke(
        {"user_input": "你好"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["final_response"] == "你好"  # 普通闲聊：不追加任何记忆说明
    assert not result.get("memory_write_candidates")
    assert any(reflector.MEMORY_INTENT_MARKER in prompt for prompt in model.seen_prompts)


def test_chat_requesting_memory_with_all_layers_disabled_is_disclosed(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """记忆层全关 + 用户明确要求记住 → 必须说明"没有保存"，不能让"我记住了"留下。"""
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        support_json_schema=False,
        memory_requested=True,  # 用户确实要求了，但没有可写目标
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"},
        context=_context_with(tmp_path, model, app_config),
    )

    answer = str(result["final_response"])
    assert "保存成功" in answer
    assert "全部关闭" in answer
    assert "好的，我记住了" not in answer  # 有未保存就替换回答，不留承诺
    assert not result.get("memory_write_candidates")


def test_chat_partial_write_is_disclosed(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """多候选只成功一部分时，不能只报"都记住了"：要说明有几条没保存（复审实证）。"""
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="chat",
        reply="好，都记住了",
        memory_requested=True,
        memory_intent=[
            # ① 能落长期（long 已启用）
            {
                "memory_type": "entity_fact",
                "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                "subject": "chaiai",
                "confidence": 0.9,
            },
            # ② 只能落中期，而中期是关闭的 → 零写入
            {"memory_type": "conversation_summary", "content": "这次聊了记忆", "confidence": 0.9},
        ],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住项目笔记是内容甲，也记住这次聊了记忆"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert len(result["memory_write_candidates"]) == 1  # 只有第 ① 条真正落库
    answer = str(result["final_response"])
    assert "只保存了 1 条" in answer and "另有 1 条" in answer
    assert "都记住了" not in answer  # 部分成功也不能保留"都记住了"


def test_reply_projection_only_targets_the_current_turn() -> None:
    """投影只认**本轮**有文本的回答：上一轮的 AI 消息不会被当成本轮目标。"""
    stale = AIMessage(content="上一轮的回答", id="a1")
    messages: list[AnyMessage] = [
        stale,
        HumanMessage(content="上一轮问题", id="h1"),
        AIMessage(content="", id="a2"),  # 本轮模型只提工具调用：没有文本
    ]
    messages.append(HumanMessage(content="本轮问题", id="h2"))

    assert projection.current_turn_ai_message(messages) is None  # 划界在前，不会回退到 a1

    messages.append(AIMessage(content="本轮回答", id="a3"))
    current = projection.current_turn_ai_message(messages)
    assert current is not None and current.id == "a3"

    projected = projection.project_messages(
        messages, {"a1": "改过的上一轮", "a3": "改过的本轮"}
    )
    assert [item.id for item in projected] == ["a1", "h1", "a2", "h2", "a3"]  # 条数、顺序不变
    assert [item.content for item in projected] == [
        "改过的上一轮",
        "上一轮问题",
        "",
        "本轮问题",
        "改过的本轮",
    ]

    # 本轮那条 AI 消息**没有文本**时仍是投影目标：否则"记忆没保存成功"这类确定性说明无处落地，
    # 页面渲染又会跳过空消息，用户什么都看不到（复审实证）。
    empty_turn: list[AnyMessage] = [
        HumanMessage(content="本轮问题", id="h2"),
        AIMessage(content="", id="a9"),
    ]
    selected = projection.current_turn_ai_message(empty_turn)
    assert selected is not None and selected.id == "a9"
    assert [
        item.text
        for item in projection.project_messages(empty_turn, {"a9": "（这条记忆没有保存成功。）"})
    ] == ["本轮问题", "（这条记忆没有保存成功。）"]
    # 但仍然绝不回退到上一轮：本轮只有用户消息时没有投影目标
    assert projection.current_turn_ai_message([HumanMessage(content="只有问题", id="h9")]) is None


def test_chat_memory_failure_keeps_checkpoint_messages_for_audit(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记忆失败只改**展示文本**：Checkpoint 里的原始回答必须保留。

    架构 §10.1：``messages`` 是审计与恢复用的原始记录；T19 的阶段验收也要求"Checkpoint 原文恢复"。
    所以失败时不能删改历史，展示文本走 ``final_response``；也不多塞一条"原回答 + 确认"，否则下一轮
    的 Prompt 窗口与摘要会看到两份近似回答。
    """

    def _boom(context: RuntimeContext) -> None:
        del context
        raise MemoryStoreError("长期库打不开（测试故意触发）")

    monkeypatch.setattr(nodes, "long_term_store", _boom)
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=_user_preference_intent(),
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    answer = str(result["final_response"])
    assert "保存成功" in answer
    assert "我记住了" not in answer  # 展示文本不得残留承诺

    replies = [item.content for item in result["messages"] if isinstance(item, AIMessage)]
    assert replies == ["好的，我记住了"]  # 原文保留，且没有插入第二条重复回答
    # 投影键必须指向本轮那条回答：页面与下一轮 Prompt 都靠它拿修正文本
    stored = [
        item for item in result["messages"] if isinstance(item, AIMessage) and item.text.strip()
    ]
    assert stored and stored[-1].id in (result.get("reply_overrides") or {})


def test_chat_memory_final_rewrite_respects_the_prompt_budget(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """收尾重写也要过预算：放不下就不调用模型，只给确定性说明并记降级事件。"""
    memory_path = tmp_path / "data" / "memories.sqlite"
    data = app_config.model_dump()
    # 预算 = 窗口 − 输出预留 = 104 token：系统提示 + 素材 + 指令必定放不下
    data["llm"].update({"context_window_tokens": 4200, "max_tokens": 4096})
    config = _config_with_long_term(AppConfig.model_validate(data), memory_path)
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=_user_preference_intent(),
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"}, context=_context_with(tmp_path, model, config)
    )

    assert "已保存到记忆：1 条" in str(result["final_response"])  # 确定性说明照常给出
    assert any("超出预算" in str(item["message"]) for item in result["error_events"])
    # 没有发起收尾重写调用（只有 chat 那一次出站），因此也没有模型措辞可用
    assert model.decision_calls == 1


def test_chat_memory_success_keeps_a_single_ai_message(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """成功时也只在 ``final_response`` 里拼确认，不往 messages 里加第二条回答。"""
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=_user_preference_intent(),
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert "已保存" in str(result["final_response"])
    replies = [item.content for item in result["messages"] if isinstance(item, AIMessage)]
    assert replies == ["好的，我记住了"]


def test_rejected_memory_promise_is_not_shown(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """审核自相矛盾（没要求记住却给候选）时，回答里那句承诺也不能直接展示。"""
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=False,  # 没要求记住
        memory_intent=_user_preference_intent(),  # 却给出候选 → 丢弃
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "今天天气不错"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    answer = str(result["final_response"])
    assert "保存成功" in answer  # 确定性的"未保存"结果
    assert "我记住了" not in answer
    assert not result.get("memory_write_candidates")


def test_duplicate_candidates_are_counted_once(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """同一条候选在一轮里重复出现时只处理一次：确认文案不能报"保存 2 条"。

    中期稳定 id 会让重复候选只留一条，若按候选条数报数，用户看到的数字与库里的记录对不上（复审实证）。
    """
    memory_path = tmp_path / "data" / "memories.sqlite"
    fact = {
        "memory_type": "entity_fact",
        "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
        "subject": "chaiai",
        "confidence": 0.9,
    }
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=[fact, dict(fact)],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住项目笔记是内容甲"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert len(result["memory_write_candidates"]) == 1
    assert "已保存到记忆：1 条" in str(result["final_response"])
    opened = long_term.open_store(memory_path)
    try:
        entity = opened.find_entity(
            entity_type="project",
            name="chaiai",
            scope=scope.SCOPE_PROJECT,
            project_id=scope.project_id_for(tmp_path),
        )
        assert entity is not None
        assert len(opened.list_facts(entity_id=entity.id)) == 1
    finally:
        opened.close()


def test_chat_mixed_success_and_rejection_reports_the_rejection_reason(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """一条成功 + 一条被拒时，说明里要给**拒绝原因**。

    复审实证：被拒候选的原因没进逐候选汇总，用户会被告知"目标记忆层当前不可用"——那是错误方向。
    """
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="chat",
        reply="好的，都记住了",
        memory_requested=True,
        memory_intent=[
            {
                "memory_type": "entity_fact",
                "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                "subject": "chaiai",
                "confidence": 0.9,
            },
            # content 不是规范 JSON → 被 memory_candidates 拒绝
            {"memory_type": "entity_fact", "content": "不是 JSON", "subject": "chaiai"},
        ],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住项目笔记是内容甲"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert len(result["memory_write_candidates"]) == 1
    answer = str(result["final_response"])
    assert "只保存了 1 条" in answer
    assert "规范 JSON" in answer  # 真实原因（候选不合法）
    assert "目标记忆层当前不可用" not in answer  # 不能错报成"层不可用"


def _task_fragment_model(model_factory: ModelFactory) -> Any:
    """一个"读文件 → 反思判定完成 → 给出一条 task_fragment 候选"的假模型。"""
    return model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "task_fragment",
                    "content": "note.txt 的内容是内容甲",
                    "subject": "读取笔记",
                    "confidence": 0.9,
                }
            ],
        },
    )


def test_medium_write_failure_does_not_block_long_term_write(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中期写入失败不影响长期：两层各自降级（原来共用一个打开与异常出口）。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"
    stub = _StubMediumStore(fail_write=True)
    monkeypatch.setattr(nodes, "medium_term_store", lambda context: stub)
    config = _config_with_medium_term(tmp_path, _config_with_long_term(app_config, memory_path))

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(tmp_path, _task_fragment_model(model_factory), config),
    )

    # 中期写失败 → 记事件、候选不登记；长期结论照样写进去
    assert any("中期记忆" in str(event["message"]) for event in _memory_write_events(result))
    assert not result.get("memory_write_candidates")
    opened = long_term.open_store(memory_path)
    try:
        assert len(opened.list_task_conclusions(project_scope=scope.project_id_for(tmp_path))) == 1
    finally:
        opened.close()


def test_long_store_failure_does_not_block_medium_write(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长期库打不开也不影响中期写入：两个方向都要独立降级。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"
    stub = _StubMediumStore()
    monkeypatch.setattr(nodes, "medium_term_store", lambda context: stub)

    def _boom(context: RuntimeContext) -> None:
        del context
        raise MemoryStoreError("长期库打不开（测试故意触发）")

    monkeypatch.setattr(nodes, "long_term_store", _boom)
    config = _config_with_medium_term(tmp_path, _config_with_long_term(app_config, memory_path))

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(tmp_path, _task_fragment_model(model_factory), config),
    )

    assert len(stub.records) == 1  # 中期片段写进去了
    assert any("长期记忆库不可用" in str(event["message"]) for event in result["error_events"])
    # 长期层**配置已启用但打不开** ≠ 未启用：它是这条候选的必需目标且失败 → 不能登记完成，
    # 下一轮还会重试（中期与长期结论都是幂等的，重试不会写重）。
    assert not result.get("memory_write_candidates")


def test_partial_write_is_not_marked_complete(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长期结论真实写入失败 → 候选**不登记完成**，下一轮还有机会补写。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"

    class _BrokenLongStore:
        """能打开、能召回（空），但写入任务结论时抛错——模拟"写入层真实失败"。"""

        def active_facts_with_entities(self, scope_keys: Any, *, now: Any = None) -> list[Any]:
            del scope_keys, now
            return []

        def list_task_conclusions(self, *, project_scope: str, limit: Any = None) -> list[Any]:
            del project_scope, limit
            return []

        def upsert_task_conclusion(self, **kwargs: Any) -> Any:
            del kwargs
            raise MemoryStoreError("结论写入失败（测试故意触发）")

    monkeypatch.setattr(nodes, "long_term_store", lambda context: _BrokenLongStore())
    config = _config_with_long_term(app_config, memory_path)

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(tmp_path, _task_fragment_model(model_factory), config),
    )

    assert not result.get("memory_write_candidates")
    assert any(
        "写入长期任务结论失败" in str(event["message"]) for event in _memory_write_events(result)
    )


def test_zero_write_candidate_is_not_marked_complete(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选的目标层没启用 → 零写入 → **不登记完成**（复审实证：只按 satisfied 会误登记）。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    stub = _StubMediumStore()
    monkeypatch.setattr(nodes, "medium_term_store", lambda context: stub)
    config = _config_with_medium_term(tmp_path, app_config)  # 只启用中期，长期保持关闭
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                    "subject": "chaiai",
                    "confidence": 0.9,
                }
            ],
        },
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"}, context=_context_with(tmp_path, model, config)
    )

    assert not stub.records  # entity_fact 不写中期
    assert not result.get("memory_write_candidates")  # 零写入 → 不登记完成
    assert any("未写入长期事实" in str(item["summary"]) for item in result["decision_trace"])


def test_long_only_conversation_summary_does_not_block_completion_logic(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """只启用长期时，``conversation_summary``（只写中期）同样零写入 → 不登记完成。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"
    config = _config_with_long_term(app_config, memory_path)  # 中期保持关闭
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "conversation_summary",
                    "content": "笔记里写的是内容甲",
                    "confidence": 0.9,
                }
            ],
        },
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"}, context=_context_with(tmp_path, model, config)
    )

    assert not result.get("memory_write_candidates")
    opened = long_term.open_store(memory_path)
    try:
        assert opened.list_task_conclusions(project_scope=scope.project_id_for(tmp_path)) == []
    finally:
        opened.close()


def test_chat_write_failure_is_disclosed_in_the_answer(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闲聊里写库失败必须说出来：模型可能已经回答过"好的，我记住了"（复审实证）。"""
    memory_path = tmp_path / "data" / "memories.sqlite"

    def _boom(context: RuntimeContext) -> None:
        del context
        raise MemoryStoreError("长期库打不开（测试故意触发）")

    monkeypatch.setattr(nodes, "long_term_store", _boom)
    model = model_factory(
        decision="chat",
        reply="好的，我记住了",
        memory_requested=True,
        memory_intent=_user_preference_intent(),
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "请记住：我习惯用 pytest"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    # 失败时**替换**回答：不能既说"我记住了"又说"没有保存成功"
    answer = str(result["final_response"])
    assert "保存成功" in answer
    assert "好的，我记住了" not in answer
    assert not result.get("memory_write_candidates")


def test_multiple_rejections_are_all_kept(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig, legacy_loop: None
) -> None:
    """旧循环写入层的多个拒绝都要保留（事件 ID 曾因相同 stage/轮次互相覆盖）。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {"memory_type": "entity_fact", "content": "不是 JSON", "subject": "chaiai"},
                # 规范 JSON 缺 key → 仍然拒绝（缺 subject 已不再算非法，见 subject 推导用例）
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project"}',
                    "subject": "chaiai",
                },
            ],
        },
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(tmp_path, model, _config_with_long_term(app_config, memory_path)),
    )

    assert len(_memory_write_events(result)) == 2


def test_source_event_ids_use_run_id_and_tool_call_id() -> None:
    """来源事件是 ``run_id:tool_call_id``：不同轮次复用同一个 ``call_1`` 不会被当成"已写过"。"""
    first = {
        "run_id": "run-a",
        "tool_events": [
            ToolEvent(tool_call_id="call_1", tool_name="read_file", run_id="run-a")
        ],
    }
    second = {
        "run_id": "run-b",
        "tool_events": [
            ToolEvent(tool_call_id="call_1", tool_name="read_file", run_id="run-b")
        ],
    }

    assert nodes._source_event_ids(first) == ("run-a:call_1",)
    assert nodes._source_event_ids(second) == ("run-b:call_1",)


def test_dirty_long_term_row_is_isolated_to_the_recall_layer(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """库里一行脏数据只让召回降级，不拖垮整轮对话（解码失败要变成领域异常）。"""
    memory_path = tmp_path / "data" / "memories.sqlite"
    config = _config_with_long_term(app_config, memory_path)
    opened = long_term.open_store(memory_path)
    try:
        entity = opened.record_entity(
            entity_type="project",
            name="chaiai",
            scope=scope.SCOPE_PROJECT,
            project_id=scope.project_id_for(tmp_path),
        )
        opened.upsert_fact(
            entity_id=entity.id,
            key="language",
            value="中文",
            confidence=0.9,
            importance=0.9,
            source="test",
        )
    finally:
        opened.close()
    with sqlite3.connect(memory_path) as connection:
        connection.execute('UPDATE facts SET "confidence" = ?', ("oops",))

    model = model_factory(decision="chat", reply="好的")
    result = builder.build_chat_graph().invoke(
        {"user_input": "我的项目用什么语言？"}, context=_context_with(tmp_path, model, config)
    )

    assert result["final_response"] == "好的"
    assert any("长期记忆召回失败" in str(event["message"]) for event in result["error_events"])
    assert result["memory_refs"] == []


def test_long_term_fact_is_recalled_in_a_new_session(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """T19 验收：同一项目下，写进长期库的事实能在**新会话**里被召回。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"
    config = _config_with_long_term(app_config, memory_path)
    writer_model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                    "subject": "chaiai",
                    "importance": 0.9,
                    "confidence": 0.9,
                }
            ],
        },
    )
    first = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(tmp_path, writer_model, config),
    )
    assert first["task_status"] == "completed"

    # 第二次是一个全新的图与全新的会话：只能靠长期库把上轮的事实找回来
    reader_model = model_factory(decision="chat", reply="好的")
    second = builder.build_chat_graph().invoke(
        {"user_input": "我的项目笔记写了什么？"},
        context=_context_with(tmp_path, reader_model, config),
    )

    references = list(second["memory_refs"])
    assert any(item.get("kind") == "long_term_fact" for item in references)
    assert any("内容甲" in str(item["content"]) for item in references)
    # 注入 Prompt 的段落按来源标注，不会把长期综合分说成"相关度"（T35：记忆已移入运行上下文快照）
    assert "长期事实" in prompt_context.build_context_snapshot_text(
        context=_context_with(tmp_path, reader_model, config),
        memory_refs=references,
    )


def test_long_term_fact_is_not_recalled_in_another_project(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """T19 验收：跨项目隔离——另一个工作区（不同 ``project_id``）召回不到。"""
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    memory_path = tmp_path / "data" / "memories.sqlite"
    config = _config_with_long_term(app_config, memory_path)
    writer_model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={
            "goal_achieved": True,
            "next_action": "finish",
            "memory_candidates": [
                {
                    "memory_type": "entity_fact",
                    "content": '{"entity_type": "project", "key": "note", "value": "内容甲"}',
                    "subject": "chaiai",
                    "confidence": 0.9,
                }
            ],
        },
    )
    builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"},
        context=_context_with(tmp_path, writer_model, config),
    )

    other = tmp_path / "other-workspace"
    other.mkdir()
    reader_model = model_factory(decision="chat", reply="好的")
    result = builder.build_chat_graph().invoke(
        {"user_input": "我的项目笔记写了什么？"},
        context=_context_with(other, reader_model, config),
    )

    assert result["memory_refs"] == []


def test_disabled_layers_do_not_create_their_stores(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """三层各自关闭时不访问对应库：既不建 Chroma 目录，也不建长期库文件。"""
    data = app_config.model_dump()
    data["memory"]["medium_term"].update(
        {"enabled": False, "path": str(tmp_path / "data" / "chroma")}
    )
    data["memory"]["long_term"].update(
        {"enabled": False, "path": str(tmp_path / "data" / "memories.sqlite")}
    )
    model = model_factory(decision="chat", reply="你好")

    result = builder.build_chat_graph().invoke(
        {"user_input": "你好"},
        context=_context_with(tmp_path, model, AppConfig.model_validate(data)),
    )

    assert result["memory_refs"] == []
    assert not (tmp_path / "data" / "chroma").exists()
    assert not (tmp_path / "data" / "memories.sqlite").exists()


def test_broken_long_term_store_is_reported_without_breaking_the_turn(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """长期库损坏：本轮照常回答，只在 ``error_events`` 里说清原因（架构 §11.6）。"""
    broken = tmp_path / "data" / "memories.sqlite"
    broken.parent.mkdir(parents=True)
    broken.write_text("这不是一个 SQLite 数据库", encoding="utf-8")
    model = model_factory(decision="chat", reply="你好")

    result = builder.build_chat_graph().invoke(
        {"user_input": "你好"},
        context=_context_with(
            tmp_path, model, _config_with_long_term(app_config, broken)
        ),
    )

    assert result["final_response"] == "你好"
    assert any(
        event["stage"] == nodes.RECALL_MEMORY and "长期记忆召回失败" in str(event["message"])
        for event in result.get("error_events", [])
    )


def test_answer_without_tool_calls_has_no_evidence(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """模型没有调用工具时不得凭空产生工具证据（哪怕它自称读过文件）。"""
    model = model_factory(decision="tool", reply="我已经读过你的文件了")

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 README"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["final_response"] == "我已经读过你的文件了"
    assert not result.get("tool_events")
    assert not result.get("tool_results")
    assert not result.get("final_evidence")
    assert result.get("reflection_result") is None


def test_edit_tool_loop_records_diff_evidence(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """编辑闭环：改文件 → 反思校验 Hash 变化与 diff → 产出 diff 类证据。"""
    target = tmp_path / "note.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    model = model_factory(
        decision="tool",
        reply="已把 value 改为 2",
        tool_calls=[
            {
                "name": "edit_file",
                "args": {
                    "path": "note.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                    "expected_sha256": sha,
                },
                "id": "call_1",
            }
        ],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "把 note.py 里的 value 改成 2"}, context=_context_with(tmp_path, model, app_config)
    )

    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert result["tool_results"][0]["status"] == "ok"
    assert result["reflection_result"].result_trust == "verified"
    evidence = result["final_evidence"][0]
    assert evidence.evidence_type == "diff"
    assert evidence.verified is True
    assert result["final_response"] == "已把 value 改为 2"


def test_delete_request_pauses_for_approval(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """删除属于强制审批动作：图暂停等待确认，且不产生任何删除（架构 §5.3、§6.5）。"""
    target = tmp_path / "note.txt"
    target.write_text("保留我", encoding="utf-8", newline="")
    model = model_factory(
        decision="tool",
        reply="无法删除该文件",
        tool_calls=[{"name": "delete_file", "args": {"path": "note.txt"}, "id": "call_1"}],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "删除 note.txt"}, context=_context_with(tmp_path, model, app_config)
    )

    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "保留我"
    payload = _interrupt_payload(result)
    assert payload["tool_name"] == "delete_file"
    assert payload["confirmation_stage"] == 1
    assert "note.txt" in str(payload["impact_summary"])
    assert result["pending_approval"]["tool_name"] == "delete_file"
    assert not result.get("tool_results")  # 还没有执行任何工具


def test_large_edit_pauses_for_approval(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """大范围修改需要人工审批：文件保持不变，图停在审批节点。"""
    target = tmp_path / "note.py"
    target.write_text("value = 1\n", encoding="utf-8", newline="")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    model = model_factory(
        decision="tool",
        reply="改动过大，被拒绝",
        tool_calls=[
            {
                "name": "edit_file",
                "args": {
                    "path": "note.py",
                    "old_text": "value = 1",
                    "new_text": "\n".join(f"line{index}" for index in range(500)),
                    "expected_sha256": sha,
                },
                "id": "call_1",
            }
        ],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "把 note.py 整个重写"}, context=_context_with(tmp_path, model, app_config)
    )

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    payload = _interrupt_payload(result)
    assert payload["tool_name"] == "edit_file"
    assert "大范围修改" in str(payload["impact_summary"])
    assert not result.get("tool_results")


def test_loaded_agents_rules_reach_the_model_prompt(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    (tmp_path / "AGENTS.md").write_text("规则甲：必须用中文回答", encoding="utf-8")
    graph = builder.build_chat_graph()
    context = _context(tmp_path, model_factory, app_config, decision="chat")

    graph.invoke({"user_input": "你好"}, context=context)

    chat_prompts = [
        prompt
        for prompt in context.model_adapter.seen_prompts  # type: ignore[attr-defined]
        if routers.ROUTE_CLASSIFIER_MARKER not in prompt
    ]
    assert chat_prompts
    assert "规则甲" in chat_prompts[-1]


def test_system_prompt_contains_mode_workspace_and_rules(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config)
    context.agents_context = "规则乙"

    prompt = prompt_context.build_stable_system_prompt(mode="chat", context=context)

    assert "## 项目规则" in prompt.system_prompt
    assert "规则乙" in prompt.system_prompt
    assert tmp_path.as_posix() in prompt.system_prompt
    assert "先调用 list_dir 或 read_file 取证" in prompt.system_prompt
    assert prompt.estimated_tokens > 0


def test_probe_detects_capabilities(model_factory: ModelFactory) -> None:
    capabilities = probe_capabilities(model_factory(decision="chat"), provider_name="fake")

    assert capabilities.tool_calling is True
    assert capabilities.structured_json_schema is True
    assert capabilities.streaming is True
    assert capabilities.as_dict()["provider_name"] == "fake"


def test_probe_reports_missing_capabilities(model_factory: ModelFactory) -> None:
    capabilities = probe_capabilities(
        model_factory(decision="chat", fail_bind_tools=True, support_json_schema=False),
        provider_name="fake",
    )

    assert capabilities.tool_calling is False
    assert capabilities.structured_json_schema is False


def test_tool_mode_fails_closed_without_tool_calling(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """不支持 Tool Calling 时不得静默退化成闲聊，要明确提示（架构 §11.7）。"""
    model = model_factory(decision="tool", reply="不该走到这里", fail_bind_tools=True)

    result = builder.build_chat_graph().invoke(
        {"user_input": "列出当前目录"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["mode"] == "tool"
    assert result["task_status"] == "failed"
    assert result["final_response"] == TOOL_CALLING_UNAVAILABLE_REPLY
    assert result["error_events"]
    assert model.decision_calls == 0  # 没有真的发起工具调用


def test_json_schema_unsupported_uses_prompt_json(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    model = model_factory(decision="tool", reply="好的", support_json_schema=False)

    context = _context_with(tmp_path, model, app_config)
    result = builder.build_chat_graph().invoke({"user_input": "列出目录"}, context=context)

    assert result["mode"] == "tool"
    capabilities = context.model_capabilities
    assert isinstance(capabilities, ModelCapabilities)
    assert capabilities.structured_json_schema is False
    assert any("JSON" in prompt for prompt in model.seen_prompts)


def test_structured_output_failure_falls_back_to_rules(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """两次解析都失败：显式暴露失败类型，并退化为规则判定。"""
    model = model_factory(
        decision="tool",
        reply="x",
        support_json_schema=False,
        invalid_classification=True,
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "帮我读取 README.md"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["mode"] == "tool"
    assert "规则兜底" in result["route_reason"]
    assert "StructuredOutputError" in result["route_reason"]


def test_call_structured_uses_prompt_json_when_schema_unsupported(
    model_factory: ModelFactory,
) -> None:
    model = model_factory(decision="chat", decision_reason="闲聊", support_json_schema=False)
    capabilities = probe_capabilities(model, provider_name="fake")

    decision = call_structured(
        model,
        [SystemMessage(content=f"{ROUTE_CLASSIFIER_MARKER}：判断")],
        RouteDecision,
        capabilities=capabilities,
    )

    assert decision.mode == "chat"
    assert decision.reason == "闲聊"


def test_structured_output_error_does_not_leak_raw_text(model_factory: ModelFactory) -> None:
    """失败信息只含结构名与失败类型，不回显模型原始输出（架构 §11.7）。"""
    model = model_factory(
        decision="chat", support_json_schema=False, invalid_classification=True
    )

    with pytest.raises(StructuredOutputError) as excinfo:
        call_structured(
            model,
            [SystemMessage(content=f"{ROUTE_CLASSIFIER_MARKER}：判断")],
            RouteDecision,
        )

    message = str(excinfo.value)
    assert "RouteDecision" in message
    assert "这不是 JSON" not in message


def test_system_prompt_without_agents_file_says_so(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config)

    prompt = prompt_context.build_stable_system_prompt(mode="tool", context=context)

    assert "未加载到任何 AGENTS 文件" in prompt.system_prompt
    assert "如果当前模式是 tool" in prompt.system_prompt


# --------------------------------------------------------------------------- T14B 记忆召回


class FakeMemoryStore:
    """只实现 recall 需要的两个方法的假记忆库（避免在路由测试里起 Chroma）。"""

    def __init__(self, hits: list[MediumTermHit]) -> None:
        self._hits = hits

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]

    def query(
        self, *, query_vector: Sequence[float], scope_keys: list[tuple[str, str]], n_results: int
    ) -> list[MediumTermHit]:
        allowed = set(scope_keys)
        return [
            hit
            for hit in self._hits
            if (hit.metadata.get("scope"), hit.metadata.get("scope_key")) in allowed
        ][:n_results]


def _memory_hit(content: str, *, scope_key: str, **metadata: object) -> MediumTermHit:
    values: dict[str, object] = {
        "scope": "project",
        "scope_key": scope_key,
        "importance": 0.9,
        "confidence": 0.9,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    values.update(metadata)
    return MediumTermHit(memory_id=f"mem-{abs(hash(content)) % 10000}", content=content, metadata=values, distance=0.0)


def _config_with_medium_term(
    tmp_path: Path, app_config: AppConfig, **overrides: object
) -> AppConfig:
    """打开中期记忆的配置副本；embedding 必须同时打开（配置校验要求两者一致）。"""
    data = app_config.model_dump()
    data["memory"]["medium_term"].update({"enabled": True, **overrides})
    data["embedding"].update(
        {
            "enabled": True,
            "provider": "onnx_local",
            "model": "fake-model",
            "cache_dir": (tmp_path / "models").as_posix(),
        }
    )
    return AppConfig.model_validate(data)


@pytest.fixture(autouse=True)
def _clear_medium_term_store_cache() -> Iterator[None]:
    """节点级 store 缓存是进程级的：每个用例前后都清掉，避免互相污染。"""
    nodes._MEDIUM_TERM_STORES.clear()
    yield
    nodes._MEDIUM_TERM_STORES.clear()


def test_recall_node_passes_through_when_disabled(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """记忆关闭时不得打开记忆库（架构 §5：关闭的层不访问相应库）。"""

    def _forbidden(context: RuntimeContext) -> object:
        raise AssertionError("记忆关闭时不应该打开中期记忆库")

    monkeypatch.setattr(nodes, "medium_term_store", _forbidden)
    context = _context(tmp_path, model_factory, app_config, decision="chat")

    result = builder.build_chat_graph().invoke({"user_input": "你好"}, context=context)

    assert result["memory_refs"] == []
    assert not result.get("error_events")


def test_recall_node_injects_memory_into_prompt(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """召回到的记忆要同时进 State 与系统提示（架构 §10.2 的"只注入相关记忆"）。"""
    store = FakeMemoryStore(
        [
            _memory_hit("用户偏好用中文回答", scope_key=scope.project_id_for(tmp_path)),
            _memory_hit("别的项目的记忆", scope_key="others"),
        ]
    )
    monkeypatch.setattr(nodes, "medium_term_store", lambda context: store)
    context = _context(
        tmp_path, model_factory, _config_with_medium_term(tmp_path, app_config), decision="chat"
    )

    result = builder.build_chat_graph().invoke({"user_input": "我偏好什么语言"}, context=context)

    assert [reference["content"] for reference in result["memory_refs"]] == ["用户偏好用中文回答"]
    prompts = [
        prompt
        for prompt in context.model_adapter.seen_prompts  # type: ignore[attr-defined]
        if routers.ROUTE_CLASSIFIER_MARKER not in prompt
    ]
    assert "## 召回的相关记忆" in prompts[-1]
    assert "用户偏好用中文回答" in prompts[-1]
    assert "别的项目的记忆" not in prompts[-1]
    assert "记忆可能过期" in prompts[-1]


def test_recall_node_records_error_event_and_keeps_turn_alive(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """召回失败不静默：记错误事件、置空记忆，本轮对话继续（架构 §11.6）。"""

    def _boom(context: RuntimeContext) -> object:
        raise MemoryStoreError("中期记忆 collection 与当前 embedding 配置不兼容")

    monkeypatch.setattr(nodes, "medium_term_store", _boom)
    context = _context(
        tmp_path,
        model_factory,
        _config_with_medium_term(tmp_path, app_config),
        decision="chat",
        reply="照常回答",
    )

    result = builder.build_chat_graph().invoke({"user_input": "你好"}, context=context)

    assert result["memory_refs"] == []
    assert result["final_response"] == "照常回答"
    messages = [event["message"] for event in result["error_events"]]
    assert any("记忆召回失败" in message and "不兼容" in message for message in messages)
    assert any(event["stage"] == nodes.RECALL_MEMORY for event in result["error_events"])


def _reference(
    content: str, *, similarity: float = 0.9, kind: str | None = None
) -> MemoryReference:
    reference = MemoryReference(
        memory_id=f"mem-{abs(hash(content)) % 10000}",
        content=content,
        scope="project",
        scope_key="p",
        similarity=similarity,
        score=similarity * 0.8,
        importance=0.5,
        confidence=0.5,
        created_at="2026-09-15T12:00:00+00:00",
        source_thread_id=None,
        expires_at=None,
    )
    if kind is not None:
        reference["kind"] = kind  # type: ignore[typeddict-unknown-key]
    return reference


def test_recall_failure_replaces_restate_advice(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """召回失败时，回答里的"你也可以直接告诉我工具名"要被**代码**替换掉。

    真实模型回归实证：提示词里已经明确禁止追问与建议重述，模型仍附带这条建议（严格口径未兑现）。
    中文措辞无法靠提示词穷尽，所以必须有机械兜底——这条用例验证的是**代码**行为，不是提示词文案。
    """
    memory_path = tmp_path / "data" / "memories.sqlite"

    def _boom(context: RuntimeContext) -> long_term.LongTermStore:
        del context
        raise MemoryStoreError("长期库打不开（测试故意触发）")

    monkeypatch.setattr(nodes, "long_term_store", _boom)
    reader = model_factory(decision="chat", reply="这次没查到。你也可以直接告诉我工具名。")

    result = builder.build_chat_graph().invoke(
        {"user_input": "我习惯用什么测试工具？"},
        context=_context_with(tmp_path, reader, _config_with_long_term(app_config, memory_path)),
    )

    answer = str(result["final_response"])

    assert "直接告诉我" not in answer  # 建议重述被拦下
    assert "无法确认" in answer  # 换成确定性说明
    assert "不需要你补充信息" in answer


def test_successful_recall_keeps_the_answer_untouched(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """对照：召回正常时同样的措辞不受影响——兜底只在"本轮召回失败"时生效。"""
    reader = model_factory(decision="chat", reply="这次没查到。你也可以直接告诉我工具名。")

    result = builder.build_chat_graph().invoke(
        {"user_input": "随便聊聊"}, context=_context_with(tmp_path, reader, app_config)
    )

    assert "直接告诉我" in str(result["final_response"])


def _plan_state(
    *, steps: int = 3, index: int = 1, completed: tuple[str, ...] = (), retry_count: int = 0
) -> dict[str, Any]:
    """构造一份带计划的最小 State（供 ``_advance_plan`` / ``_apply_retry_limit`` 直调）。"""
    return {
        "run_id": "run-1",
        "plan": [
            nodes.PlanStep(
                id=f"run-1:step-{item + 1}",
                description=f"第 {item + 1} 步",
                status="running" if item == index else "pending",
                retry_count=retry_count if item == index else 0,
            )
            for item in range(steps)
        ],
        "current_step_index": index,
        "completed_steps": list(completed),
    }


def test_multi_step_plan_continues_after_the_first_step(
    legacy_loop,
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """T20：计划有多步时，**完成第一步不等于完成整个任务**，三步必须真的走完。

    模型基于已验证的工具结果想 ``finish``，机械规则仍改成 ``continue`` 并推进索引——否则整图会在
    第二步收尾、第三步永远不执行，而 ``task_status`` 还是 ``running``，用户拿到一个半截任务
    （复审实证：修复前这条用例只断言到索引 1 就结束）。
    """
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        plan_steps=["读取 note.txt", "总结内容", "给出结论"],
        reflection={"goal_achieved": True, "next_action": "finish"},
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取并总结 note.txt"},
        context=_context_with(tmp_path, model, app_config),
    )

    plan = list(result["plan"])
    assert [step.description for step in plan] == ["读取 note.txt", "总结内容", "给出结论"]
    assert [step.status for step in plan] == ["completed", "completed", "completed"]
    assert result["current_step_index"] == 2  # 停在最后一步：没有"下一步"可推进
    assert [step.id for step in plan] == list(result["completed_steps"])  # 三步都记了完成
    assert result["task_status"] == "completed"
    # 中途的反思被机械改成 continue：出现过"完成一步不等于完成整个任务"的规范化说明
    assert any(
        "完成一步不等于完成整个任务" in record["summary"]
        for record in result["decision_trace"]
    )


def test_route_after_decision_keeps_working_on_an_unfinished_plan() -> None:
    """计划还没走完时不能直接收尾；有工具调用优先，单步计划照旧（验收基线：不回归）。"""

    def step(number: int, status: str) -> nodes.PlanStep:
        return nodes.PlanStep(
            id=f"run-1:step-{number}",
            description=f"第 {number} 步",
            status=status,  # type: ignore[arg-type]
        )

    multi = [step(1, "completed"), step(2, "running"), step(3, "pending")]

    assert routers.route_after_decision({"plan": multi}) == routers.ROUTE_REFLECT
    # 有工具调用时仍走安全门：执行优先于"计划没走完"这个判断
    assert (
        routers.route_after_decision(
            {"plan": multi, "pending_tool_calls": [{"name": "read_file"}]}
        )
        == routers.ROUTE_WITH_TOOLS
    )
    # 多步计划全部完成 → 正常收尾
    assert (
        routers.route_after_decision({"plan": [step(1, "completed"), step(2, "completed")]})
        == routers.ROUTE_WITHOUT_TOOLS
    )
    # 单步计划不回归：旧行为就是"模型直接回答即收尾"
    assert (
        routers.route_after_decision({"plan": [step(1, "running")]})
        == routers.ROUTE_WITHOUT_TOOLS
    )


def test_replanning_inserts_an_adjustment_step() -> None:
    """T20：``plan_adjustment`` 必须**真的改计划**（只写进摘要不算），且已完成步骤不被重写。"""
    state = _plan_state(index=1, completed=("run-1:step-1",))
    decision = ReflectionDecision(
        goal_achieved=False,
        next_action="continue",
        plan_adjustment="先列目录确认结构，再决定读哪个文件",
    )

    progress = nodes._advance_plan(
        state, goal_achieved=False, next_action="continue", decision=decision
    )

    assert progress.plan[1].status == "failed"  # 失败步骤被标记
    assert progress.plan[2].description == "先列目录确认结构，再决定读哪个文件"  # 插入的调整步骤
    assert progress.plan[2].status == "running"
    assert progress.current_step_index == 2  # 索引指向新步骤
    assert progress.next_action == "continue"
    assert progress.completed_steps == ()  # 失败的步骤不算完成
    assert progress.plan[0] == state["plan"][0]  # 已完成步骤原样保留


def test_replanning_at_the_step_cap_rewrites_in_place() -> None:
    """计划已达拆步上限时不再插入新步骤：改为原地重做并计一次重试（否则任务永远收不了尾）。

    实测：真实模型把一句问句拆成 4 步后又连续插入 5 个调整步骤，计划一路涨到 9 步，怎么"继续"
    都走不完。
    """
    cap = reflector.MAX_PLAN_STEPS
    state = _plan_state(steps=cap, index=cap - 1, completed=tuple(f"run-1:step-{i}" for i in range(1, cap)))
    decision = ReflectionDecision(
        goal_achieved=False,
        next_action="continue",
        plan_adjustment="改用按行号区间读取，别整文件读",
    )

    progress = nodes._advance_plan(
        state, goal_achieved=False, next_action="continue", decision=decision
    )

    assert len(progress.plan) == cap  # 不再增长
    assert progress.plan[cap - 1].description == "改用按行号区间读取，别整文件读"  # 计划真的变了
    assert progress.plan[cap - 1].status == "running"
    assert progress.plan[cap - 1].retry_count == 1  # 计一次重试，交给重试上限兜底
    assert progress.current_step_index == cap - 1
    assert progress.next_action == "retry"
    assert "上限" in (progress.note or "")


def test_retry_budget_is_per_step() -> None:
    """T20：重试上限按 ``PlanStep.retry_count`` 判定，进入下一步后不沿用上一步的次数。"""
    used_up = _plan_state(steps=1, index=0, retry_count=2)
    fresh = _plan_state(index=1, retry_count=0, completed=("run-1:step-1",))

    blocked, note = nodes._apply_retry_limit(used_up, "retry", max_retries=2)
    allowed, _ = nodes._apply_retry_limit(fresh, "retry", max_retries=2)

    assert blocked == "ask_user" and note is not None
    assert allowed == "retry"  # 第二步从 0 开始，不受第一步影响


def test_historical_completed_steps_do_not_leak_into_a_new_task() -> None:
    """T20：历史 ``completed_steps`` 只是追加记录，不能让新任务误判"已完成"。

    步骤 id 含 ``run_id``，旧任务的步骤 id 不会命中新任务的步骤。
    """
    state = {
        "run_id": "run-new",
        "plan": [
            nodes.PlanStep(id="run-new:step-1", description="新任务第一步", status="running")
        ],
        "current_step_index": 0,
        "completed_steps": ["run-old:step-1"],  # 上一次任务留下的记录
    }
    decision = ReflectionDecision(goal_achieved=True, next_action="finish")

    progress = nodes._advance_plan(
        state, goal_achieved=True, next_action="finish", decision=decision
    )

    assert progress.completed_steps == ("run-new:step-1",)  # 新步骤被正常记为完成
    assert progress.goal_achieved is True  # 单步计划：确实做完了
    assert "run-old:step-1" not in progress.completed_steps  # 不是"再次完成"


def test_unconfirmed_side_effect_asks_the_user_to_verify(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T20：副作用可能已发生、但 Checkpoint 未确认时**不自动重放**，改为请用户核对（架构 §4.2/§251）。

    这类调用不能靠 State 证明"恰好执行过一次"，所以即使本轮工具结果看起来正常，也要停下来。
    """
    (tmp_path / "note.txt").write_text("内容甲", encoding="utf-8")
    monkeypatch.setattr(nodes, "unconfirmed_calls", lambda state: ["call-ghost"])
    model = model_factory(
        decision="tool",
        reply="读完了",
        tool_calls=[{"name": "read_file", "args": {"path": "note.txt"}, "id": "call_1"}],
        reflection={"goal_achieved": True, "next_action": "finish"},
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt"}, context=_context_with(tmp_path, model, app_config)
    )

    assert result["reflection_result"].next_action == "ask_user"
    assert result["reflection_result"].goal_achieved is False
    answer = str(result["final_response"])
    assert "核对" in answer  # 明确要求人工核对，而不是继续自动执行
    assert "call-ghost" in answer
    # 本轮已结束、在等用户补充：既不是 completed，也不是"还在跑"。
    assert result["task_status"] == "blocked"


def test_memory_section_distinguishes_failure_from_empty() -> None:
    """召回失败与"查了没有结果"必须分开表述：混说会让模型断言它并不知道的结论。"""
    assert "没有召回相关记忆" in prompt_context.render_memory_section([])

    failed = prompt_context.render_memory_section([], recall_failed=True)

    assert "不可用" in failed
    assert "不代表" in failed
    assert "没有召回相关记忆" not in failed
    # 真实页面回归补充：模型曾附带"你可以直接告诉我偏好"这类可选建议。检索失败是本轮的技术问题，
    # 不该让用户重述——口径是连这类建议也不要（用户裁定）。
    assert "不要**追问用户" in failed
    assert "不要**建议用户" in failed


def test_recall_failure_is_not_reported_as_no_memory(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库里有记录、但召回失败时：Prompt 要说"这次没查到"，页面要给出可读原因。

    真实实证：中期/长期库不可用时 ``memory_refs`` 为空，Prompt 却写"本轮没有召回相关记忆"，
    模型于是回答"我没有关于你偏好的记忆"——与页面下方的故障提示自相矛盾（库里其实有记录）。
    """
    memory_path = tmp_path / "data" / "memories.sqlite"
    store = long_term.open_store(memory_path)
    try:
        entity = store.record_entity(
            entity_type="user",
            name="local",
            scope=scope.SCOPE_PROJECT,
            project_id=scope.project_id_for(tmp_path),
        )
        store.upsert_fact(
            entity_id=entity.id,
            key="test_runner",
            value="pytest",
            confidence=0.9,
            importance=0.8,
            source="test",
        )
    finally:
        store.close()

    def _boom(context: RuntimeContext) -> long_term.LongTermStore:
        del context
        raise MemoryStoreError("长期库打不开（测试故意触发）")

    monkeypatch.setattr(nodes, "long_term_store", _boom)
    reader = model_factory(decision="chat", reply="好的")

    result = builder.build_chat_graph().invoke(
        {"user_input": "我习惯用什么测试工具？"},
        context=_context_with(tmp_path, reader, _config_with_long_term(app_config, memory_path)),
    )

    assert result["memory_refs"] == []  # 召回确实失败了
    assert any("不可用" in prompt for prompt in reader.seen_prompts)
    assert not any("本轮没有召回相关记忆" in prompt for prompt in reader.seen_prompts)

    # 页面侧：T33 之后页面只看 DTO——节点名不进页面，映射成公开错误码与文案（§15.5、§15.8），
    # 因此"召回失败"与"RAG 失败"在页面上是可区分的两件事，但都不暴露内部 stage。
    rows = app_projection.project_messages_view(result)
    errors = [row.error for row in rows if row.kind == "error"]
    assert len(errors) == 1
    assert errors[0].code == app_errors.MEMORY_RETRIEVAL_FAILED
    assert "记忆" in errors[0].title


def test_memory_label_hides_long_term_scores() -> None:
    """长期条目**不带数字**：综合分是内部排序量。

    实测两轮：模型先把它说成"置信度"（该条记录其实是 0.95），后来又在显式记忆问答里直接复述
    "综合分 0.63"。口径是不向用户报任何数值，所以长期标签干脆不给数字；中期的相关度保留——它是
    真实语义相似度，含义明确，能帮模型判断贴合程度。
    """
    section = prompt_context.render_memory_section(
        [
            _reference("用户习惯用 pytest", similarity=0.66, kind="long_term_fact"),
            _reference("上次的结论", kind="task_conclusion"),
            _reference("中期记忆"),
        ]
    )

    assert "[长期事实]" in section
    assert "[任务结论]" in section
    assert "0.66" not in section  # 长期排序分不出现在 Prompt 里
    assert "综合分" not in section
    assert "相关度 0.90" in section  # 中期仍标注语义相关度


def test_classifier_prompt_routes_memory_questions_to_chat(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """回忆型问题（"我习惯用什么…"）要按闲聊处理。

    真实实证：直接问"我习惯用什么测试工具？"时模型选了工具模式，检查空目录后反而追问用户，
    尽管该轮已经召回了记忆。答案本来就在系统提示的召回段落里，不需要访问文件。
    """
    prompt = routers.build_classifier_prompt(
        _context_with(tmp_path, model_factory(decision="chat", reply="x"), app_config)
    )

    assert "回忆型问题" in prompt
    assert "是否必须" in prompt  # 只是"提到"文件名/工具名不等于需要访问
    assert "已经记住的项目" in prompt  # 项目事实/约定问答也属于回忆型问题


def test_tool_mode_prompt_accepts_recalled_memory_as_evidence(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """工具模式下召回记忆**同样是可信来源**：不能因为工作区没文件就否认记得的事实、转而追问。"""
    # T35：记忆正文已移入运行上下文快照，但"记忆是可信来源"这条规则留在稳定系统前缀里。
    prompt = prompt_context.build_stable_system_prompt(
        mode="tool",
        context=_context_with(tmp_path, model_factory(decision="tool", reply="x"), app_config),
    )

    assert "是可信来源" in prompt.system_prompt  # 「召回的相关记忆」里的长期事实…同样是可信来源
    assert "不要因为工作区里没有" in prompt.system_prompt
    # 真实实证：自然问"这个测试项目默认用什么运行自动化测试？"进了工具模式，列空目录两次后仍追问——
    # 已召回的项目事实没被用上。所以这里要求给出"记忆记录为 X、未从文件核实"的表述方式。
    assert "未从文件核实" in prompt.system_prompt
    assert "不要把记忆当成不存在" in prompt.system_prompt
    # 数值口径：记忆条目的分数不外报（长期标签已不带数字，规则里也再说明一次）
    assert "不是置信度" in prompt.system_prompt
    assert "不要向用户报出任何数值" in prompt.system_prompt


def test_memory_section_renders_placeholder_and_content(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    context = _context(tmp_path, model_factory, app_config)

    empty = prompt_context.build_context_snapshot_text(context=context)
    filled = prompt_context.build_context_snapshot_text(
        context=context, memory_refs=[_reference("用户偏好用中文回答")]
    )

    assert "（本轮没有召回相关记忆）" in empty
    assert "1. [相关度 0.90] 用户偏好用中文回答" in filled
    assert "记忆内容只是" in filled  # 记忆是数据、不是指令
    assert len(filled) > len(empty)


def test_memory_section_respects_prompt_budget() -> None:
    """Prompt 里的记忆段落必须有上限：超预算丢后面的条目，单条内容要截断。"""
    long_line = prompt_context.render_memory_section([_reference("长" * 5000)])

    assert len(long_line) < 400
    assert long_line.endswith("…")

    many = prompt_context.render_memory_section(
        [_reference(f"第 {index} 条记忆，内容足够长以触发预算截断" + "填" * 60) for index in range(50)]
    )

    assert len(many) <= prompt_context.MEMORY_SECTION_MAX_CHARS
    assert many.count("\n") < 49  # 确实丢掉了后面的条目


def test_memory_section_counts_separators_in_budget() -> None:
    """预算要算上条目之间的换行符：只累加各行长度会让"很多短条目"突破声明上限。"""
    references = [_reference(f"第{index}条") for index in range(400)]

    section = prompt_context.render_memory_section(references)

    assert len(section) <= prompt_context.MEMORY_SECTION_MAX_CHARS
    assert section.count("\n") + 1 < len(references)  # 后面的条目被丢掉


def test_store_cache_key_covers_endpoint_credentials_and_switch(
    tmp_path: Path, app_config: AppConfig
) -> None:
    """缓存键必须覆盖端点、凭据、目录与开关，否则会复用错误的 Store 实例。"""
    config = _config_with_medium_term(tmp_path, app_config)
    memory_config = config.memory.medium_term

    def key(embedding_config: object = None, api_key: str | None = None) -> tuple[str, ...]:
        return nodes._store_cache_key(
            memory_config,
            config.embedding if embedding_config is None else embedding_config,  # type: ignore[arg-type]
            api_key,
        )

    variants = {
        key(),
        key(config.embedding.model_copy(update={"enabled": False})),
        key(config.embedding.model_copy(update={"provider": "openai_compatible"})),
        key(config.embedding.model_copy(update={"base_url": "https://other.example.com/v1"})),
        key(config.embedding.model_copy(update={"api_key_env": "OTHER_KEY"})),
        key(config.embedding.model_copy(update={"cache_dir": "E:/other-models"})),
        key(config.embedding.model_copy(update={"local_files_only": True})),
        key(api_key="key-one"),
        key(api_key="key-two"),
    }

    assert len(variants) == 9
    assert "key-one" not in str(key(api_key="key-one"))  # 原始 Key 不进缓存键
    assert len(nodes._credential_fingerprint("key-one")) == 64  # 完整 SHA-256，不截断


def test_store_is_created_once_under_concurrency(
    tmp_path: Path,
    model_factory: ModelFactory,
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发首次召回只允许建一个 Store（否则两个会话各加载一份模型）。"""
    opened: list[object] = []

    def slow_open(*args: object, **kwargs: object) -> FakeMemoryStore:
        time.sleep(0.05)
        store = FakeMemoryStore([])
        opened.append(store)
        return store

    monkeypatch.setattr(medium_term, "open_store", slow_open)
    context = _context_with(
        tmp_path, model_factory(decision="chat"), _config_with_medium_term(tmp_path, app_config)
    )
    results: list[object] = []

    threads = [
        threading.Thread(target=lambda: results.append(nodes.medium_term_store(context)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(opened) == 1
    assert len({id(item) for item in results}) == 1


# --------------------------------------------------------------------------- T21 按需代码检索


def _rag_enabled(app_config: AppConfig, **overrides: object) -> AppConfig:
    data = app_config.model_dump()
    data["rag"].update({"enabled": True, **overrides})
    return AppConfig.model_validate(data)


def _node_runtime(context: RuntimeContext) -> Any:
    """构造节点需要的 ``Runtime`` 包装（只暴露 ``context``）。"""

    class _Runtime:
        def __init__(self, ctx: RuntimeContext) -> None:
            self.context = ctx

    return _Runtime(context)


def test_retrieve_code_node_passes_through_when_disabled(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory, monkeypatch
) -> None:
    """**显式**关掉 ``rag.enabled`` 时不访问文件系统、不探测 ``rg``、不记错误事件（架构 §5.3）。

    2026-10-01：``rag.enabled`` 的默认值由 ``false`` 改为 ``true``，这条不再靠默认值——
    改成显式关闭，测的是**关闭这一支的行为**本身（默认值由配置层用例钉）。
    """
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("关闭时不应调用检索")

    monkeypatch.setattr(nodes, "retrieve_code", boom)
    context = _context(tmp_path, model_factory, _rag_enabled(app_config, enabled=False))

    update = nodes.retrieve_code_node(
        {"run_id": "run-1", "normalized_request": "看看 routers.py"}, _node_runtime(context)
    )

    assert update == {"rag_refs": []}


def test_retrieve_code_node_writes_references(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory
) -> None:
    """工具分支的检索节点把命中写进 ``rag_refs``（含 rank 与行区间）。"""
    (tmp_path / "mod.py").write_text("decide_mode = 1\n", encoding="utf-8")
    context = _context(tmp_path, model_factory, _rag_enabled(app_config))

    update = nodes.retrieve_code_node(
        {"run_id": "run-1", "normalized_request": "decide_mode 在哪"}, _node_runtime(context)
    )

    references = update["rag_refs"]
    assert [reference["path"] for reference in references] == ["mod.py"]
    assert references[0]["rank"] == 1
    assert references[0]["line_start"] == 1


def test_retrieve_code_node_reports_failure_without_breaking_the_graph(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory, monkeypatch
) -> None:
    """检索失败只记一条可见错误事件，既不抛异常，也不伪称"检索过了"（架构 §11.6）。"""
    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("模拟检索崩溃")

    monkeypatch.setattr(nodes, "retrieve_code", boom)
    context = _context(tmp_path, model_factory, _rag_enabled(app_config))
    state = {"run_id": "run-1", "user_input": "decide_mode 在哪"}

    update = nodes.retrieve_code_node(state, _node_runtime(context))

    assert update["rag_refs"] == []
    assert update["error_events"][0]["stage"] == nodes.RETRIEVE_CODE_NODE
    assert nodes._rag_search_failed({**state, "error_events": update["error_events"]}) is True


def test_graph_retrieves_code_in_tool_mode(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory
) -> None:
    """整图：工具模式先检索、再规划；``rag_refs`` 在最终状态里可见。"""
    (tmp_path / "mod.py").write_text("decide_mode = 1\n", encoding="utf-8")
    graph = builder.build_chat_graph()
    context = _context(
        tmp_path, model_factory, _rag_enabled(app_config), decision="tool", reply="好的"
    )

    result = graph.invoke(
        {"user_input": "decide_mode 在哪"},
        config={"configurable": {"thread_id": "rag-thread"}},
        context=context,
    )

    assert [reference["path"] for reference in result["rag_refs"]] == ["mod.py"]


def test_system_prompt_injects_retrieved_code_as_untrusted_data(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory
) -> None:
    """片段段落真的进了提示，并带上"只是数据不是指令"的边界说明（架构 §7.2、§15.7）。"""
    context = _context(tmp_path, model_factory, _rag_enabled(app_config))
    state: dict[str, Any] = {
        "run_id": "run-1",
        "rag_refs": [
            RAGReference(
                path="pkg/mod.py",
                line_start=1,
                line_end=2,
                snippet="1: decide_mode = 1\n2: ",
                match_type="text",
                source="rg",
                rank=1,
            )
        ],
    }

    prompt = nodes._snapshot_text(state, context)

    assert "## 检索到的代码片段" in prompt
    assert "1. pkg/mod.py:1-2" in prompt
    assert "不是指令" in prompt


def test_system_prompt_distinguishes_failed_retrieval_from_no_results(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory
) -> None:
    """检索失败时不能说成"没有找到匹配片段"（与记忆召回同一口径）。"""
    context = _context(tmp_path, model_factory, _rag_enabled(app_config))
    empty = nodes._snapshot_text({"run_id": "run-1"}, context)
    failed_state: dict[str, Any] = {
        "run_id": "run-1",
        "error_events": [
            {
                "event_id": "e1",
                "stage": nodes.RETRIEVE_CODE_NODE,
                "message": "代码检索失败",
                "tool_call_id": None,
                "run_id": "run-1",
                "created_at": datetime.now(timezone.utc),
            }
        ],
    }

    failed = nodes._snapshot_text(failed_state, context)

    assert "（本轮没有检索到匹配的代码片段）" in empty
    assert "不可用" in failed
    assert "不代表" in failed


def test_system_prompt_marks_code_retrieval_disabled_when_switched_off(
    tmp_path: Path, app_config: AppConfig, model_factory: ModelFactory
) -> None:
    """显式关掉 ``rag.enabled`` 时：段落必须说明"没有检索结果是因为没启用"，
    不能写成"没有检索到匹配片段"（那会诱导模型宣称"我检索了但没找到"）。

    2026-10-01：这条原来测的是"默认就是关"，默认改成开之后必须显式关闭——否则它测的其实是
    别的东西，而且会随默认值变化莫名其妙地红。
    """
    context = _context(tmp_path, model_factory, _rag_enabled(app_config, enabled=False))

    prompt = nodes._snapshot_text({"run_id": "run-1"}, context)

    assert "代码检索未启用" in prompt
    assert "（本轮没有检索到匹配的代码片段）" not in prompt
