"""Memory output must be writable before leaving the bounded JSON retry chain."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from local_agent.graph import builder, nodes
from local_agent.graph.state import MemoryCandidate
from local_agent.llm.protocol import StructuredOutputError
from local_agent.memory.schemas import ORIGIN_OWNER
from local_agent.reflection.memory_candidates import RejectedCandidate, plan_writes
from local_agent.reflection.reflector import (
    CandidateDecision,
    TaskMemoryCandidate,
    extract_task_memory,
)

# The exact unescaped inner value in the 2026-10-06 B1/turn-2 Checkpoint.
BAD_FLOW = (
    '{"entity_type":"project","key":"FLOW","value":"("HTTP", "PLAN", "TOOL", '
    '"APPROVAL", "ANSWER")，定义于 demo/controller.py:3，即消息处理顺序 '
    'HTTP→PLAN→TOOL→APPROVAL→ANSWER"}'
)
FLOW = {"entity_type": "project", "key": "FLOW",
        "value": ["HTTP", "PLAN", "TOOL", "APPROVAL", "ANSWER"]}


def _reply(content):
    return AIMessage(content=json.dumps({"memory_candidates": [{
        "memory_type": "entity_fact", "content": content, "confidence": .95,
        "importance": .7,
    }]}, ensure_ascii=False))


def test_unescaped_entity_fact_is_repaired_once_preserving_prefix_and_scope(tmp_path):
    calls = []
    outputs = iter([_reply(BAD_FLOW), _reply(FLOW)])

    def generate(messages):
        calls.append(messages)
        return next(outputs)

    prefix = [HumanMessage(content="confirmed read_file result: FLOW")]
    decision = extract_task_memory(RunnableLambda(generate), prefix=prefix)
    assert len(calls) == 2
    assert all(call[:len(prefix)] == prefix for call in calls)
    candidate = decision.memory_candidates[0]
    assert json.loads(candidate.content) == FLOW
    writes = plan_writes(
        # origin 由写入链路显式给出（不取模型输出）：T52 起它是必填字段。
        MemoryCandidate(
            **candidate.model_dump(), source_event_ids=["real"], origin=ORIGIN_OWNER
        ),
        workspace_root=tmp_path,
    )
    assert not isinstance(writes, RejectedCandidate)
    assert writes[0].scope == "project"
    assert writes[0].payload.value == FLOW["value"]


def test_bad_entity_fact_still_fails_after_two_attempts_without_raw_output():
    calls = []

    def generate(messages):
        calls.append(messages)
        return _reply(BAD_FLOW)

    with pytest.raises(StructuredOutputError) as error:
        extract_task_memory(RunnableLambda(generate), prefix=[HumanMessage(content="history")])
    assert len(calls) == 2
    assert BAD_FLOW not in str(error.value)


@pytest.mark.parametrize("content", [json.dumps(FLOW), FLOW])
def test_valid_string_and_object_fact_need_only_one_call(content):
    calls = []

    def generate(messages):
        calls.append(messages)
        return _reply(content)

    result = extract_task_memory(RunnableLambda(generate), prefix=[])
    assert len(calls) == 1
    assert json.loads(result.memory_candidates[0].content) == FLOW


@pytest.mark.parametrize("data", [
    {"memory_type": "conversation_summary", "content": "   "},
    {"memory_type": "task_fragment", "content": {"value": "not a fragment"}},
    {"memory_type": "entity_fact", "content": {"entity_type": "project", "key": "missing"}},
    {"memory_type": "entity_fact", "content": {"entity_type": "project", "key": " ", "value": 1}},
    {"memory_type": "entity_fact", "content": {"entity_type": "project", "key": "x", "value": float("nan")}},
    {"memory_type": "entity_fact", "content": '{"entity_type":"project","key":"x","value":NaN}'},
    {"memory_type": "task_fragment", "content": "confirmed", "confidence": 2},
    {"memory_type": "task_fragment", "content": "confirmed", "importance": float("nan")},
])
def test_bad_candidate_fails_before_write(data):
    with pytest.raises(ValidationError):
        TaskMemoryCandidate.model_validate(data)


def test_canonical_fact_keeps_json_values_and_project_default(tmp_path):
    payload = {"entity_type": "user", "key": "preference",
               "value": {"quotes": '"quoted"', "list": [None, False, 1, "中文\n行"]}}
    decision = CandidateDecision.model_validate({"memory_type": "entity_fact", "content": payload})
    assert json.loads(decision.content) == payload
    writes = plan_writes(
        MemoryCandidate(
            **decision.model_dump(), source_event_ids=["real"], origin=ORIGIN_OWNER
        ),
        workspace_root=tmp_path,
        user_id="actual-user",
    )
    assert not isinstance(writes, RejectedCandidate)
    assert writes[0].scope == "project"  # No new cross-project authorization.


def test_task_memory_failure_preserves_answer_and_reports_error(tmp_path, app_config, model_factory):
    (tmp_path / "note.txt").write_text("confirmed source", encoding="utf-8")
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = True
    app_config.memory.long_term.path = str(tmp_path / "memory.sqlite")
    model = model_factory(decision="tool", reply="读取完成，结论已根据文件核实。", tool_calls=[{
        "name": "read_file", "args": {"path": "note.txt"}, "id": "actual-read",
    }], reflection={"memory_candidates": [{"memory_type": "entity_fact", "content": BAD_FLOW}]})
    context = builder.build_runtime_context(app_config=app_config, model_adapter=model,
                                           workspace_root=tmp_path, current_cwd=tmp_path)
    result = builder.build_chat_graph().invoke({"user_input": "读取 note.txt"}, context=context)
    assert result["agent_loop_version"] == 1
    assert result["task_status"] == "completed"
    assert "结论" in result["final_response"]
    assert not result.get("memory_write_candidates")
    errors = [e for e in result.get("error_events", [])
              if e["stage"] == f"{nodes.TASK_MEMORY_NODE}:memory_intent"]
    assert len(errors) == 1
    assert "StructuredOutputError" in errors[0]["message"]
    assert BAD_FLOW not in errors[0]["message"]
    assert sum("这不是完成度反思，不决定继续/重试" in p for p in model.seen_prompts) == 2
