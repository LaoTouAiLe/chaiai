"""Regressions for the three prefix losses captured by the paired live experiment."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from local_agent.context.token_budget import estimate_message_tokens
from local_agent.graph import builder, nodes
from local_agent.llm.models import ModelCapabilities
from local_agent.reflection.reflector import extract_task_memory


def _context(tmp_path, app_config, model_factory):
    return builder.build_runtime_context(
        app_config=app_config, model_adapter=model_factory(),
        workspace_root=tmp_path, current_cwd=tmp_path,
    )


def test_next_turn_keeps_previous_snapshot_and_all_previous_request_bytes(
    tmp_path, app_config, model_factory,
):
    context = _context(tmp_path, app_config, model_factory)
    first = {
        "agent_loop_version": 1, "run_id": "one", "run_user_message_id": "one:user",
        "mode": "tool", "messages": [HumanMessage(content="first task", id="one:user")],
    }
    old_snapshot = nodes._runtime_snapshot(first, context)
    sent = nodes._main_conversation_prefix(first, context, old_snapshot)
    second = {
        **first, "run_id": "two", "run_user_message_id": "two:user",
        "task_goal": "different goal", "runtime_context_snapshots": [old_snapshot],
        "messages": [*first["messages"], AIMessage(content="first answer"),
                     HumanMessage(content="second task", id="two:user")],
    }
    new_snapshot = nodes._runtime_snapshot(second, context)
    following = nodes._main_conversation_prefix(second, context, new_snapshot)
    assert [m.model_dump() for m in following[:len(sent)]] == [m.model_dump() for m in sent]
    assert following[-2].text == new_snapshot.canonical_text
    assert nodes._snapshot_tokens(second, context, messages=second["messages"]) == (
        estimate_message_tokens(HumanMessage(content=old_snapshot.canonical_text))
        + estimate_message_tokens(HumanMessage(content=new_snapshot.canonical_text))
    )
    assert len(second["messages"]) == 3  # Reconstruction does not mutate the audit log.


def test_tool_character_budget_does_not_slide_when_model_window_has_room(
    tmp_path, app_config, model_factory,
):
    app_config.memory.compression.enabled = True
    app_config.memory.compression.preserve_recent_tool_steps = 1
    app_config.memory.compression.tool_step_budget_chars = 1
    app_config.llm.context_window_tokens = 1_048_576
    app_config.llm.max_tokens = 512
    context = _context(tmp_path, app_config, model_factory)
    messages = [HumanMessage(content="task", id="r:user")]
    for index in range(3):
        identity = str(index)
        messages.extend([
            AIMessage(content="", tool_calls=[{
                "name": "read_file", "id": identity, "args": {"path": "note.txt"},
            }]), ToolMessage(content="verified data " * 600, tool_call_id=identity),
        ])
    state = {"agent_loop_version": 1, "run_id": "r", "run_user_message_id": "r:user",
             "mode": "tool", "messages": messages}
    roomy = nodes._prompt_window(state, context)
    assert roomy.omitted_tool_steps == 0
    assert [m.model_dump() for m in roomy.messages] == [m.model_dump() for m in messages]
    app_config.llm.context_window_tokens = 8_000
    pressured = nodes._prompt_window(state, context)
    assert pressured.omitted_tool_steps == 0  # New loop uses recorded compaction, not transient omission.
    assert pressured.tool_result_overrides
    legacy = nodes._prompt_window({**state, "agent_loop_version": 0}, context)
    assert legacy.omitted_tool_steps > 0


def test_memory_json_retries_tool_request_without_executing_it_or_changing_capabilities():
    seen = []
    outputs = iter([
        AIMessage(content='{"memory_candidates": []}', tool_calls=[{
            "name": "delete_file", "id": "never-execute", "args": {"path": "note.txt"},
        }]), AIMessage(content=json.dumps({"memory_candidates": []})),
    ])

    def generate(messages):
        seen.append(messages)
        return next(outputs)

    prefix = [HumanMessage(content="confirmed task history")]
    capabilities = ModelCapabilities(tool_calling=True, structured_json_schema=True)
    result = extract_task_memory(
        RunnableLambda(generate), prefix=prefix, capabilities=capabilities,
        estimated_tools_tokens=100, tools_schema_fingerprint="unchanged-tools",
    )
    assert result.memory_candidates == []
    assert len(seen) == 2
    assert all(call[:len(prefix)] == prefix for call in seen)
    assert capabilities.structured_json_schema  # The role preference does not degrade the provider.


def test_snapshot_stays_before_kept_tool_history_after_user_is_covered(tmp_path, app_config, model_factory):
    from local_agent.context.token_budget import ContextBudget

    app_config.memory.compression.enabled = True
    context = _context(tmp_path, app_config, model_factory)
    user = HumanMessage(content="调查", id="r:user")
    state = {"agent_loop_version": 1, "run_id": "r", "run_user_message_id": "r:user",
             "mode": "tool", "messages": [user], "conversation_summary": "已核对部分证据"}
    frozen = nodes._runtime_snapshot(state, context)
    state["runtime_context_snapshots"] = [frozen]
    state["messages"] = [user, AIMessage(content="", tool_calls=[{
        "name": "read_file", "id": "read", "args": {"path": "note.txt"}}]),
        ToolMessage(content="old evidence", tool_call_id="read", name="read_file")]
    state["context_budget"] = ContextBudget(context_window_tokens=1048576,
        completion_reserve_tokens=512, prompt_budget_tokens=1048064,
        estimated_prompt_tokens=1000, usage_ratio=0.01, trigger_ratio=0.8, triggered=True,
        window_messages=2, total_messages=3, summary_chars=8, summary_covered_messages=1)
    sent = nodes._main_conversation_prefix(state, context, frozen)
    following = nodes._main_conversation_prefix({**state, "messages": [
        *state["messages"], AIMessage(content="已核对")
    ]}, context, frozen)
    assert [m.model_dump() for m in following[:len(sent)]] == [m.model_dump() for m in sent]
    assert sent[2].text == frozen.canonical_text
    assert isinstance(sent[3], AIMessage)
    assert isinstance(sent[4], ToolMessage)

    next_turn = {**state, "run_id": "next", "run_user_message_id": "next:user",
                 "messages": [*state["messages"], AIMessage(content="已核对"),
                              HumanMessage(content="继续", id="next:user")]}
    new_snapshot = nodes._runtime_snapshot(next_turn, context)
    next_sent = nodes._main_conversation_prefix(next_turn, context, new_snapshot)
    assert [m.model_dump() for m in next_sent[:len(sent)]] == [m.model_dump() for m in sent]
    assert next_sent[-2].text == new_snapshot.canonical_text
