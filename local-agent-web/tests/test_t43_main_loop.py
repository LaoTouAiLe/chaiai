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
from local_agent.graph import builder
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
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])

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
    assert len(result["final_evidence"]) == 1
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
    assert result["task_status"] == "blocked"
    assert [step.status for step in result["plan"]] == ["running", "pending"]
    assert not result["reflection_result"].goal_achieved


def test_plan_cannot_complete_tools_whose_results_model_has_not_seen(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), plan_steps=["读取", "解释"], script=[
        _tools(_call("read_file", "read", path="note.txt"), _call("update_plan", "premature", plan=[
            {"step": "读取", "status": "completed"}, {"step": "解释", "status": "completed"},
        ])), AIMessage(content="计划更新未成功，需要继续处理。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "blocked"
    assert not result.get("completed_steps")
    assert next(item for item in result["tool_events"] if item.tool_name == "update_plan").status == "error"


def test_failed_read_is_returned_to_main_model_and_can_recover(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "missing", path="missing.txt")),
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="换用正确路径后已读取。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert [event.status for event in result["tool_events"]] == ["denied", "ok"]
    assert model.decision_calls == 3


def test_final_answer_after_failed_tool_is_not_task_success(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "missing", path="missing.txt")), AIMessage(content="未能读取。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "blocked"
    assert not result["reflection_result"].goal_achieved
    assert result["final_response"] == "未能读取。"


def test_denied_tool_is_returned_to_main_model(tmp_path, app_config) -> None:
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("not_registered", "denied")), AIMessage(content="该工具无法执行。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert model.decision_calls == 2
    assert result["task_status"] == "blocked"
    assert result["final_response"] == "该工具无法执行。"


def test_completed_plan_step_cannot_be_removed() -> None:
    state = {
        "run_id": "r", "plan": [PlanStep(id="r:step-1", description="读取", status="completed")],
        "reflection_result": ReflectionResult(goal_achieved=False, result_trust="verified"),
    }
    update = PlanUpdate.model_validate({"plan": [{"step": "修改", "status": "in_progress"}]})
    with pytest.raises(ValueError, match="已完成"):
        apply_plan_update(state, update)


def test_plan_tool_returns_validated_json() -> None:
    from local_agent.tools.plan_tools import build_plan_tools

    output = build_plan_tools()[0].invoke({"plan": [{"step": "读取", "status": "in_progress"}]})
    assert json.loads(output)["plan"] == [{"step": "读取", "status": "in_progress"}]


def test_explicit_plan_and_completed_work_survive_approval_restart(tmp_path, app_config) -> None:
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
    assert first["task_status"] == "blocked"
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
        AIMessage(content="这条消息不应再调用模型生成。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "blocked"
    assert model.decision_calls == 2
    assert "停止重试" in result["final_response"]


def test_no_tool_answer_can_finish_on_last_budgeted_model_call(tmp_path, app_config) -> None:
    app_config.agent.max_tool_loops = 2
    model = LoopModel(messages=iter([]), script=[
        _tools(_call("read_file", "read", path="note.txt")), AIMessage(content="内容已确认。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed"
    assert result["loop_count"] == 2


def test_same_batch_completion_is_rejected_even_after_earlier_verified_work() -> None:
    state = {
        "run_id": "r", "plan": [PlanStep(id="r:step-1", description="修改")],
        "reflection_result": ReflectionResult(goal_achieved=False, result_trust="verified"),
        "pending_tool_calls": [_call("edit_file", "edit", path="note.txt")],
    }
    with pytest.raises(ValueError, match="同批"):
        apply_plan_update(state, PlanUpdate.model_validate({
            "plan": [{"step": "修改", "status": "completed"}],
        }))


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
