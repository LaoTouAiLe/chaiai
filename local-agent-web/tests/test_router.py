"""T04 验收：AGENTS 规则加载与模式路由（架构 §5、§7）。

覆盖扫描边界（Git 根、工作区上界）、多层与 override 优先级、截断、以及
``mode_router`` 的 LLM 分类与规则兜底。全部使用临时目录与假模型，不联网。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import SystemMessage, ToolMessage

from local_agent.config import AgentsConfig, AppConfig
from local_agent.context import agents_loader, prompt_context
from local_agent.graph import builder, routers
from local_agent.graph.routers import ROUTE_CLASSIFIER_MARKER, RouteDecision
from local_agent.graph.state import RuntimeContext
from local_agent.llm.models import (
    TOOL_CALLING_UNAVAILABLE_REPLY,
    ModelCapabilities,
    probe_capabilities,
)
from local_agent.llm.protocol import StructuredOutputError, call_structured

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
        "compression_node",
        "planner_node",
        "agent_decision_node",
        "safety_gate_node",
        "approval_interrupt_node",
        "tool_execution_node",
        "reflection_node",
        "completion_router",
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
    assert result["loop_count"] == 2
    assert [event.status for event in result["tool_events"]] == ["ok"]
    assert result["tool_results"][0]["status"] == "ok"
    assert "文件内容甲" in result["tool_results"][0]["output_preview"]
    assert result["reflection_result"].result_trust == "verified"
    assert result["reflection_result"].goal_achieved is False  # T05 不做完成度判断
    assert [evidence.verified for evidence in result["final_evidence"]] == [True]
    # 工具结果确实回填给了模型
    assert any("文件内容甲" in prompt for prompt in model.seen_prompts)
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert result["final_response"] == "我读到的是：文件内容甲"
    assert result["task_status"] == "completed"


def test_denied_tool_is_reported_back_to_model(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    """未接入/未放行的工具直接拒绝（删除这类动作走审批，见下一个用例）。"""
    model = model_factory(
        decision="tool",
        reply="好的，我无法执行该操作",
        tool_calls=[{"name": "unknown_tool", "args": {}, "id": "call_1"}],
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


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any]:
    """取出挂起中的审批载荷。"""
    raw = result.get("__interrupt__")
    assert raw, "期望图暂停在人工审批"
    return dict(raw[0].value)


def test_loop_limit_stops_the_cycle(
    tmp_path: Path, model_factory: ModelFactory, app_config: AppConfig
) -> None:
    config = _config_with_agent(app_config, max_tool_loops=1)
    model = model_factory(
        decision="tool",
        reply="不该走到这里",
        tool_calls=[{"name": "list_dir", "args": {"path": "."}, "id": "call_1"}],
    )

    result = builder.build_chat_graph().invoke(
        {"user_input": "列出目录"}, context=_context_with(tmp_path, model, app_config=config)
    )

    assert result["loop_count"] == 1
    assert result["task_status"] == "failed"
    assert result["final_response"] == routers.LOOP_LIMIT_REPLY


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

    prompt = prompt_context.build_system_prompt(
        mode="chat", context=context, route_reason="LLM 分类：闲聊"
    )

    assert "## 项目规则" in prompt.system_prompt
    assert "规则乙" in prompt.system_prompt
    assert tmp_path.as_posix() in prompt.system_prompt
    assert "不假设已经读取或修改了任何本地文件" in prompt.system_prompt
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

    prompt = prompt_context.build_system_prompt(mode="tool", context=context)

    assert "未加载到任何 AGENTS 文件" in prompt.system_prompt
    assert "如果当前模式是 tool" in prompt.system_prompt
