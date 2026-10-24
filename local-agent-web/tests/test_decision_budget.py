"""Live decision budget is visible at the request tail, without rewriting history or limits."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime
from pydantic import Field
from test_t43_main_loop import LoopModel, _call, _run, _tools

from local_agent.context.token_budget import estimate_messages_tokens
from local_agent.graph import builder, nodes
from local_agent.llm.usage_recording import request_fingerprints
from local_agent.persistence import checkpoint

MARKER = "【本次决策额度】"


class BudgetModel(LoopModel):
    requests: list[list] = Field(default_factory=list)

    def _generate(self, messages, **kwargs):
        self.requests.append(list(messages))
        return super()._generate(messages, **kwargs)


def test_live_budget_reserves_plan_update_and_answer_within_original_limit(tmp_path, app_config):
    app_config.agent.max_tool_loops = 4
    model = BudgetModel(messages=iter([]), script=[
        _tools(_call("update_plan", "plan", plan=[{"step": "读取", "status": "in_progress"}])),
        _tools(_call("read_file", "read", path="note.txt")),
        _tools(_call("update_plan", "done", plan=[{"step": "读取", "status": "completed"}])),
        AIMessage(content="已经核对文件内容。"),
    ])
    result = _run(tmp_path, app_config, model)
    assert result["task_status"] == "completed" and result["loop_count"] == 4
    assert model.decision_calls == 4
    for index, request in enumerate(model.requests, 1):
        tail = request[-1]
        assert isinstance(tail, SystemMessage) and tail.text.startswith(MARKER)
        assert f"本次为第{index}/4次决策，含本次还可调用{5-index}次" in tail.text
    assert "task_control" in model.requests[2][-1].text
    assert "用task_control明确声明完成" in model.requests[-1][-1].text
    assert all(request[0].text == model.requests[0][0].text for request in model.requests)
    for previous, current in zip(model.requests, model.requests[1:]):
        assert current[:len(previous)] == previous
    assert all(MARKER not in message.text for message in result["messages"])
    assert all(MARKER not in item.canonical_text for item in result["runtime_context_snapshots"])


def test_budget_tail_changes_dynamic_fingerprint_without_invalidating_prefix():
    prefix = [SystemMessage(content="稳定系统前缀"), HumanMessage(content="之前的问题"),
              AIMessage(content="之前的回答"), HumanMessage(content="当前任务")]
    before = request_fingerprints([*prefix, SystemMessage(content=f"{MARKER}第1/20次")])
    after = request_fingerprints([*prefix, SystemMessage(content=f"{MARKER}第2/20次")])
    assert before[:2] == after[:2] == request_fingerprints(prefix)[:2]
    assert before[2] != after[2]


def test_budget_cues_rebuild_after_checkpoint_restart_without_changing_provider_metadata(tmp_path, app_config):
    app_config.memory.medium_term.enabled = False
    app_config.memory.long_term.enabled = False
    (tmp_path / "note.txt").write_text("verified", encoding="utf-8")
    model = BudgetModel(messages=iter([]), script=[
        _tools(_call("read_file", "read", path="note.txt")),
        AIMessage(content="已核对", response_metadata={"provider_field": "retained"}),
    ])
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    config = {"configurable": {"thread_id": "budget-restart"}}
    path = tmp_path / "data/checkpoints.sqlite"
    saver = checkpoint.open_checkpointer(path)
    graph = builder.build_chat_graph(saver)
    result = graph.invoke({"user_input": "读 note.txt", "session_mode": "agent"}, config=config, context=context)
    assert result["task_status"] == "completed"
    expected = nodes._main_conversation_prefix(result, context, nodes._runtime_snapshot(result, context))
    saver.conn.close()
    reopened = checkpoint.open_checkpointer(path)
    try:
        restored = dict(builder.build_chat_graph(reopened).get_state(config).values)
        actual = nodes._main_conversation_prefix(restored, context, nodes._runtime_snapshot(restored, context))
        assert actual == expected
        assert actual[:len(model.requests[-1])] == model.requests[-1]
        assert next(m for m in reversed(restored["messages"]) if isinstance(m, AIMessage) and m.tool_calls).response_metadata["provider_field"] == "retained"
        assert len([message for message in actual if message.text.startswith(MARKER)]) == 2
    finally:
        reopened.conn.close()


def test_budget_tail_is_counted_in_prompt_usage_without_changing_shared_prefix(tmp_path, app_config):
    app_config.memory.compression.enabled = False
    model = BudgetModel(messages=iter([]), script=[AIMessage(content="独立回答。")])
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    state = {"agent_loop_version": 1, "mode": "tool", "session_mode": "agent", "run_id": "r",
             "run_user_message_id": "r:user", "loop_count": 17, "max_loop_count": 20,
             "messages": [HumanMessage(content="核对进度", id="r:user"),
                          AIMessage(content="之前的回复", response_metadata={
                              "local_agent_decision_budget": f"{MARKER}历史额度指令",
                          })]}
    runtime = Runtime(context=context)
    usage = nodes.compression_node(state, runtime)
    snapshot = nodes._runtime_snapshot(state, context)
    shared = nodes._main_conversation_prefix(state, context, snapshot)
    nodes.agent_decision_node({**state, **usage}, runtime)
    request = model.requests[0]
    assert request[:-1] == shared and request[-1].text.startswith(MARKER)
    assert "第18/20次决策，含本次还可调用3次" in request[-1].text
    expected = estimate_messages_tokens(request) + nodes._tool_definition_tokens(state, context)
    assert usage["token_usage"].prompt_tokens == expected
    assert usage["context_budget"].estimated_prompt_tokens == expected
    token_usage = usage["token_usage"]
    assert expected == token_usage.system_prompt_tokens + token_usage.window_messages_tokens + token_usage.tool_definition_tokens


@pytest.mark.parametrize("version,mode,session", [(0, "tool", "agent"), (1, "chat", "chat")])
def test_legacy_and_chat_requests_do_not_receive_agent_budget_tail(tmp_path, app_config, version, mode, session):
    model = BudgetModel(messages=iter([]), script=[AIMessage(content="回答。")])
    context = builder.build_runtime_context(
        app_config=app_config, model_adapter=model, workspace_root=tmp_path, current_cwd=tmp_path,
    )
    state = {"agent_loop_version": version, "mode": mode, "session_mode": session,
             "loop_count": 0, "max_loop_count": 4, "messages": [HumanMessage(content="问题")]}
    nodes.agent_decision_node(state, Runtime(context=context))
    assert not any(MARKER in message.text for message in model.requests[0])
