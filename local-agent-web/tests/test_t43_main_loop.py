"""T43：按明确脚本验证主模型循环、计划控制和假完成拦截。"""

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import RoutingFakeModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langgraph.types import Command
from pydantic import Field

from local_agent.config import AppConfig
from local_agent.graph import builder, nodes, routers
from local_agent.graph.plan_updates import apply_plan_update
from local_agent.graph.state import PlanStep, ReflectionResult
from local_agent.llm.stream_context import use_sink
from local_agent.persistence import checkpoint
from local_agent.tools.plan_tools import PlanUpdate
from local_agent.ui.decision_streaming import stream_decision


class LoopModel(RoutingFakeModel):
    script: list[AIMessage] = Field(default_factory=list)
    task_memory: list[dict[str, Any]] = Field(default_factory=list)
    schemas: list[str] = Field(default_factory=list)

    def _generate(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        if any("这不是完成度反思，不决定继续/重试" in message.text for message in messages):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps({
                "memory_candidates": self.task_memory,
            }, ensure_ascii=False)))])
        self.decision_calls += 1
        self.seen_prompts.append("\n".join(message.text for message in messages))
        if not self.script:
            raise AssertionError("主模型出现未预期的额外调用")
        response = self.script.pop(0)
        if (self.explicit_task_control and "task_control" in self.bound_tools
                and not response.tool_calls and response.text.strip()
                and not any("不要调用任何工具" in m.text or "不要再次发起工具调用" in m.text for m in messages)):
            response = response.model_copy(update={"content": "", "tool_calls": [{
                "name": "task_control", "id": f"finish-{self.decision_calls}", "type": "tool_call",
                "args": {"action": "complete", "message": response.text},
            }]})
        return ChatResult(generations=[ChatGeneration(message=response)])

    def with_structured_output(self, schema, **kwargs):  # type: ignore[no-untyped-def]
        self.schemas.append(schema.__name__)
        if schema.__name__ in {"ReflectionDecision", "PlanDraft"}:
            raise AssertionError("新循环不得调用完成度判官或独立规划模型")
        if schema.__name__ == "TaskMemoryDecision":
            return RunnableLambda(lambda _: schema.model_validate({
                "memory_candidates": self.task_memory,
            }))
        return super().with_structured_output(schema, **kwargs)


def _call(name: str, identity: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "id": identity, "args": args}


def _tools(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def _run(tmp_path: Path, config: AppConfig, model: LoopModel) -> dict[str, Any]:
    config.memory.medium_term.enabled = False
    config.memory.long_term.enabled = False
    (tmp_path / "note.txt").write_text("verified content", encoding="utf-8")
    context = builder.build_runtime_context(
        app_config=config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    return builder.build_chat_graph().invoke(
        {"user_input": "读取 note.txt 并根据结果回答", "session_mode": "agent"},
        context=context,
    )


def test_main_model_reads_tool_result_and_answers_once(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="文件已确认。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert result["final_response"] == "文件已确认。"
    assert model.decision_calls == 2
    assert "verified content" in model.seen_prompts[-1]
    assert "ReflectionDecision" not in model.schemas
    assert "PlanDraft" not in model.schemas
    assert result["plan"] == []


@pytest.mark.parametrize("first_tool", ["read_file", "not_registered", None])
def test_call_limit_reports_progress_without_inventing_a_denial(
    tmp_path, app_config, first_tool,
) -> None:
    """预算耗尽与曾经出错分开；收尾保留成功结果和真实错误。"""
    script = []
    if first_tool:
        args = {"path": "missing.txt"} if first_tool == "read_file" else {}
        script.append(_tools(_call(first_tool, "initial", **args)))
    script.append(_tools(_call("read_file", "recovered", path="note.txt")))
    app_config.agent.max_tool_loops = len(script)
    script.append(AIMessage(content="已读取 note.txt；剩余修改尚未执行。"))
    model = LoopModel(messages=iter([]), script=script)

    result = _run(tmp_path, app_config, model)

    assert result["task_status"] == "failed"
    assert result["loop_count"] == app_config.agent.max_tool_loops
    assert model.decision_calls == app_config.agent.max_tool_loops + 1
    assert result["final_response"].startswith(routers.LOOP_LIMIT_REPLY)
    assert "已读取 note.txt" in result["final_response"]
    prompt = model.seen_prompts[-1]
    assert nodes.LOOP_LIMIT_FEEDBACK_INSTRUCTION in prompt
    assert nodes.TOOL_FEEDBACK_INSTRUCTION not in prompt
    assert "verified content" in prompt
    assert result["tool_results"][-1]["status"] == "ok"
    if first_tool:
        assert result["tool_results"][0]["status"] in {"error", "denied"}


def test_call_limit_explanation_failure_preserves_the_stop_reason(tmp_path, app_config) -> None:
    app_config.agent.max_tool_loops = 1
    # 收尾请求没有预置响应，模型抛错；只能降级说明，不得再执行动作。
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "read", path="note.txt")),
    ])

    result = _run(tmp_path, app_config, model)

    assert result["task_status"] == "failed"
    assert result["final_response"].startswith(routers.LOOP_LIMIT_REPLY)
    assert len(result["tool_results"]) == 1
    assert result["tool_results"][0]["status"] == "ok"
    assert result["pending_tool_calls"] == []
    assert any("说明生成降级" in event["message"] for event in result["error_events"])


def test_explicit_plan_updates_are_persisted_and_complete(tmp_path, app_config) -> None:
    steps = [{"step": "读取", "status": "in_progress"}, {"step": "解释", "status": "pending"}]
    model = LoopModel(messages=iter([]), plan_steps=["读取", "解释"], script=[
        _tools(_call("update_plan", "start", plan=steps)),
        _tools(_call("read_file", "read", path="note.txt")),
        _tools(_call("update_plan", "done", plan=[
            {"step": "读取", "status": "completed"}, {"step": "解释", "status": "completed"},
        ])), AIMessage(content="两步已完成。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert [step.status for step in result["plan"]] == ["completed", "completed"]
    assert len(result["completed_steps"]) == 2
    assert len(result["final_evidence"]) == 0
    assert all(item.evidence_type != "update_plan" for item in result["final_evidence"])


def test_successful_tool_does_not_automatically_complete_plan(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), plan_steps=["读取", "修改"], script=[
        _tools(_call("update_plan", "start", plan=[
            {"step": "读取", "status": "in_progress"},
            {"step": "修改", "status": "pending"},
        ])),
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="已读，修改尚未执行。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert [step.status for step in result["plan"]] == ["running", "pending"]
    assert result["reflection_result"].goal_achieved


def test_plan_cannot_complete_tools_whose_results_model_has_not_seen(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), plan_steps=["读取", "解释"], script=[
        _tools(_call("read_file", "read", path="note.txt"), _call("update_plan", "premature", plan=[
            {"step": "读取", "status": "completed"}, {"step": "解释", "status": "completed"},
        ])), AIMessage(content="计划更新未成功，需要继续处理。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert len(result["completed_steps"]) == 2
    assert next(item for item in result["tool_events"] if item.tool_name == "update_plan").status == "ok"


def test_failed_read_is_returned_to_main_model_and_can_recover(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "missing", path="missing.txt")),
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="换用正确路径后已读取。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert [event.status for event in result["tool_events"] if event.tool_name != "task_control"] == ["error", "ok"]
    assert model.decision_calls == 3


def test_different_failed_methods_can_continue_and_success_resets_retries(tmp_path, app_config) -> None:
    app_config.agent.max_retries_per_step = 1
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "one", path="missing-one.txt")),
        _tools(_call("read_file", "two", path="missing-two.txt")),
        _tools(_call("read_file", "three", path="missing-three.txt")),
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="已找到并读取。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert model.decision_calls == 5
    assert result["retry_count"] == 0


def test_shell_failure_has_error_status_and_recovery_can_finish(tmp_path, app_config, monkeypatch) -> None:
    from local_agent.security.process_guard import ProcessOutcome, ProcessRegistry

    outcomes = iter([
        ProcessOutcome(exit_code=1, stdout=b"", stderr=b"failure"),
        ProcessOutcome(exit_code=0, stdout=b"recovered", stderr=b""),
    ])
    monkeypatch.setattr(ProcessRegistry, "run", lambda *_, **__: next(outcomes))
    app_config.workspace.external_paths.mode = "danger-full-access"
    app_config.rag.enabled = False  # 隔离 Shell 的两次结果，不让自动检索的 rg 消耗替身。
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("run_shell", "failed", command='python -c "raise SystemExit(1)"')),
        _tools(_call("run_shell", "recovered", command="python --version")),
        AIMessage(content="调整后已成功。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert [event.status for event in result["tool_events"] if event.tool_name != "task_control"] == ["error", "ok"]
    assert result["task_status"] == "completed"
    assert result["retry_count"] == 0


def test_final_answer_after_failed_tool_is_not_task_success(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "missing", path="missing.txt")), AIMessage(content="未能读取。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert result["reflection_result"].goal_achieved
    assert result["final_response"] == "未能读取。"


def test_denied_tool_is_returned_to_main_model(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("not_registered", "denied")), AIMessage(content="该工具无法执行。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert model.decision_calls == 2
    assert result["task_status"] == "completed"
    assert result["final_response"] == "该工具无法执行。"


def test_web_budget_stops_calls_in_the_same_batch_and_returns_feedback(tmp_path, app_config, monkeypatch) -> None:
    from local_agent.tools import web_tools

    contacted = []

    def transport(url, *_):
        contacted.append(url)
        return web_tools.WebResponse(url, 200, "text/html", '<h2><a href="https://example.com/">Source</a></h2>')

    monkeypatch.setattr(web_tools, "_default_transport", transport)
    app_config.web_access.provider = "bing_html"
    app_config.web_access.max_search_calls = 1
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("web_search", "one", query="first"), _call("web_search", "two", query="second")),
        AIMessage(content="已停止补查；现有资料不足。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert len(contacted) == 1
    assert [event.status for event in result["tool_events"] if event.tool_name != "task_control"] == ["ok", "error"]
    assert "检索预算已用完" in model.seen_prompts[-1]
    assert result["final_response"] == "已停止补查；现有资料不足。"


def test_web_ssrf_rejection_reaches_model_and_does_not_create_sources(tmp_path, app_config) -> None:
    from local_agent.application.projection import project_tool_rows

    model = LoopModel(messages=iter([]), script=[
        _tools(_call("web_fetch", "private", url="http://127.0.0.1/private")),
        AIMessage(content="该地址属于本机，已被安全策略拒绝，未取得资料。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert "被安全策略拒绝" in model.seen_prompts[-1]
    assert result["final_response"] == "该地址属于本机，已被安全策略拒绝，未取得资料。"
    assert result["tool_events"][0].status == "denied"
    assert all(not row.web_sources for _, row in project_tool_rows(result))


def test_completed_plan_step_can_be_removed() -> None:
    state = {
        "run_id": "r", "plan": [PlanStep(id="r:step-1", description="读取", status="completed")],
        "reflection_result": ReflectionResult(goal_achieved=False, result_trust="verified"),
    }
    update = PlanUpdate.model_validate({"plan": [{"step": "修改", "status": "in_progress"}]})
    changed = apply_plan_update(state, update)
    assert [step.description for step in changed["plan"]] == ["修改"]


def test_plan_tool_returns_validated_json() -> None:
    from local_agent.tools.plan_tools import build_plan_tools

    output = build_plan_tools()[0].invoke({"plan": [{"step": "读取", "status": "in_progress"}]})
    assert json.loads(output)["plan"] == [{"step": "读取", "status": "in_progress"}]


def test_explicit_plan_and_completed_work_survive_approval_restart(tmp_path, app_config) -> None:
    app_config.security.require_approval.delete_file = True
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    (tmp_path / "note.txt").write_text("verified content", encoding="utf-8")
    steps = [{"step": "读取", "status": "in_progress"}, {"step": "删除", "status": "pending"}]
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("update_plan", "start", plan=steps)),
        _tools(_call("read_file", "read", path="note.txt")),
        _tools(_call("update_plan", "progress", plan=[
            {"step": "读取", "status": "completed"}, {"step": "删除", "status": "in_progress"},
        ])),
        _tools(_call("delete_file", "delete", path="note.txt")),
        _tools(_call("update_plan", "done", plan=[
            {"step": "读取", "status": "completed"}, {"step": "删除", "status": "completed"},
        ])), AIMessage(content="读取后按批准删除。"),
    ])
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    database = tmp_path / "resume.sqlite"
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    config = {"configurable": {"thread_id": "planned-approval"}}
    waiting = graph.invoke(
        {"user_input": "读取 note.txt 然后删除它", "session_mode": "agent"},
        config=config, context=context,
    )
    assert waiting.get("__interrupt__")
    assert [step.status for step in waiting["plan"]] == ["completed", "running"]
    assert (tmp_path / "note.txt").is_file()
    reopened = builder.build_chat_graph(checkpoint.open_checkpointer(database))
    done = reopened.invoke(Command(resume={"decision": "approve"}), config=config, context=context)
    assert done["task_status"] == "completed"
    assert [step.status for step in done["plan"]] == ["completed", "completed"]
    assert len(done["completed_steps"]) == 2
    assert not (tmp_path / "note.txt").exists()
    assert len([event for event in done["tool_events"] if event.tool_name == "delete_file"]) == 1
    assert "ReflectionDecision" not in model.schemas


def test_new_task_does_not_inherit_previous_plan_or_failed_evidence(tmp_path, app_config) -> None:
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "missing", path="missing.txt")),
        AIMessage(content="该文件不存在。"), AIMessage(content="这是新问题的回答。"),
    ])
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    graph = builder.build_chat_graph(checkpoint.open_checkpointer(tmp_path / "turns.sqlite"))
    config = {"configurable": {"thread_id": "turns"}}
    first = graph.invoke(
        {"user_input": "读取 missing.txt", "session_mode": "agent"}, config=config, context=context,
    )
    assert first["task_status"] == "completed"
    second = graph.invoke(
        {"user_input": "解释概念，不需要读取文件", "session_mode": "agent"}, config=config, context=context,
    )
    assert second["task_status"] == "completed"
    assert second["plan"] == []
    assert second["final_response"] == "这是新问题的回答。"
    assert second["loop_count"] == 1


def test_failed_calls_are_bounded_even_when_safety_clears_pending_calls(tmp_path, app_config) -> None:
    app_config.agent.max_retries_per_step = 1
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "missing-1", path="missing.txt")),
        _tools(_call("read_file", "missing-2", path="missing.txt")),
        AIMessage(content="已经停止重试：文件不存在，无法确认其内容。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert model.decision_calls == 3  # 两次尝试后仅生成说明，不再执行工具
    assert "停止重试" in result["final_response"]


def test_no_tool_answer_can_finish_on_last_budgeted_model_call(tmp_path, app_config) -> None:
    app_config.agent.max_tool_loops = 2
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="内容已确认。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert result["loop_count"] == 2


def test_same_batch_completion_is_model_controlled() -> None:
    state = {
        "run_id": "r", "plan": [PlanStep(id="r:step-1", description="修改")],
        "reflection_result": ReflectionResult(goal_achieved=False, result_trust="verified"),
        "pending_tool_calls": [_call("edit_file", "edit", path="note.txt")],
    }
    changed = apply_plan_update(state, PlanUpdate.model_validate({
        "plan": [{"step": "修改", "status": "completed"}],
    }))
    assert changed["plan"][0].status == "completed"


@pytest.mark.parametrize("with_tools", [False, True])
def test_decision_stream_preserves_calls_usage_and_shows_intermediate_text(with_tools) -> None:
    class Sink:
        def __init__(self):
            self.text = ""
        def push(self, text):
            self.text += text
        def discard(self):
            self.text = ""

    class Model:
        def stream(self, messages):
            yield AIMessageChunk(content="最终回答" if not with_tools else "工具前的中间话")
            if with_tools:
                yield AIMessageChunk(content="", tool_call_chunks=[{
                    "name": "read_file", "args": '{"path":"note.txt"}', "id": "r", "index": 0,
                }])
            yield AIMessageChunk(content="", usage_metadata={
                "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            }, additional_kwargs={"reasoning_content": "不可展示的推理"})
    sink = Sink()
    with use_sink(sink):
        result = stream_decision(
            Model(), [], capabilities=None, enabled=True,
            estimated_prompt_tokens=120, tools_schema_fingerprint="tools",
        )
    assert isinstance(result.message, AIMessage) and not isinstance(result.message, AIMessageChunk)
    assert result.message.usage_metadata["input_tokens"] == 100
    assert "不可展示" not in sink.text
    assert sink.text == ("工具前的中间话" if with_tools else "最终回答")
    if with_tools:
        assert result.message.tool_calls[0]["args"] == {"path": "note.txt"}


@pytest.mark.parametrize("cancel", [False, True])
def test_control_answer_streams_before_arguments_close_and_cancellation_keeps_visible_text(cancel):
    from local_agent.cancellation import CancellationToken

    token = CancellationToken()

    class Sink:
        text = ""
        def push(self, text):
            self.text += text
        def discard(self):
            self.text = ""

    sink = Sink()

    class Model:
        def stream(self, messages):
            yield AIMessageChunk(content="", tool_call_chunks=[{
                "name": "task_control", "args": '{"action":"complete","message":"真实',
                "id": "finish", "index": 0,
            }])
            assert sink.text == "真实"
            if cancel:
                token.cancel("用户取消")
            yield AIMessageChunk(content="", tool_call_chunks=[{
                "name": None, "args": '回答"}', "id": None, "index": 0,
            }])

    with use_sink(sink):
        result = stream_decision(Model(), [], capabilities=None, enabled=True, cancellation=token,
            estimated_prompt_tokens=100, tools_schema_fingerprint="tools")
    if cancel:
        assert result.cancelled and result.message.text == "真实" and not result.message.tool_calls
    else:
        assert sink.text == "真实回答"
        assert result.message.tool_calls[0]["args"]["message"] == "真实回答"
