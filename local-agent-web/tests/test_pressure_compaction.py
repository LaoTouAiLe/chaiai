"""DSH 压力链路：完整证据、保存替换、SQLite 重启、完整工具前缀摘要。"""

import json
import sqlite3
from contextlib import closing

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph
from langgraph.runtime import Runtime

from local_agent.graph import builder, nodes
from local_agent.graph.state import AgentState, RuntimeContext
from local_agent.memory.short_term import select_prompt_window, tool_output_key
from local_agent.persistence.checkpoint import build_checkpoint_serializer
from local_agent.tools.file_tools import read_file_content
from local_agent.tools.result_types import ReadFileResult


def _context(tmp_path, app_config, model_factory, *, capacity=1048576, keep=3):
    app_config.memory.compression.enabled = True
    app_config.memory.compression.preserve_recent_tool_steps = keep
    app_config.memory.compression.tool_step_budget_chars = 1
    app_config.llm.context_window_tokens = capacity
    app_config.llm.max_tokens = 512
    return builder.build_runtime_context(
        app_config=app_config, model_adapter=model_factory(),
        workspace_root=tmp_path, current_cwd=tmp_path,
    )


def _state(count=1, *, body_size=16000):
    messages = [HumanMessage(content="只读调查", id="r:user")]
    for index in range(count):
        identity = str(index)
        content = "".join(f"value_{n} = '" + "x" * 80 + "'\n" for n in range(body_size // 100))
        raw = ReadFileResult(path=f"f{index}.py", content=content, line_count=len(content.splitlines()),
                             first_line=1, last_line=len(content.splitlines())).model_dump_json()
        messages.extend([
            AIMessage(content="", id=f"a{index}", tool_calls=[{
                "name": "read_file", "id": identity, "args": {"path": f"f{index}.py"},
            }]),
            ToolMessage(content=raw, id=f"t{index}", name="read_file", tool_call_id=identity),
        ])
    return {"agent_loop_version": 1, "run_id": "r", "run_user_message_id": "r:user",
            "mode": "tool", "messages": messages}


def test_roomy_context_keeps_long_evidence_and_entire_history(tmp_path, app_config, model_factory):
    context = _context(tmp_path, app_config, model_factory)
    state = _state(55)
    window = nodes._prompt_window(state, context)
    assert len(window.messages) == 111  # 不因 100 条窗口限制重写前缀。
    assert not window.tool_result_overrides and not window.omitted_tool_steps
    for message in window.messages:
        if isinstance(message, ToolMessage):
            data = json.loads(message.content)
            assert not data["truncated"] and data["omitted_ranges"] == []
            assert "160: value_159" in data["content"]
    assert "1: value_0" not in state["messages"][-1].content


def _compression_graph(saver):
    graph = StateGraph(AgentState, context_schema=RuntimeContext)
    graph.add_node("compress", nodes.compression_node)
    graph.set_entry_point("compress")
    graph.set_finish_point("compress")
    return graph.compile(checkpointer=saver)


def test_pressure_replacement_survives_sqlite_restart_and_lower_pressure(
    tmp_path, app_config, model_factory,
):
    context = _context(tmp_path, app_config, model_factory, capacity=6000)
    state = _state()
    config = {"configurable": {"thread_id": "pressure"}}
    database = tmp_path / "pressure.sqlite"
    with closing(sqlite3.connect(database, check_same_thread=False)) as connection:
        graph = _compression_graph(SqliteSaver(connection, serde=build_checkpoint_serializer()))
        result = graph.invoke(state, config=config, context=context)
        assert result["tool_output_overrides"]
        assert any("保存 1 条工具正文替换" in event["summary"] for event in result["decision_trace"])
        sent = nodes._prompt_window(result, context)
        clipped = json.loads(sent.messages[-1].content)
        assert clipped["truncated"] and clipped["omitted_ranges"]
        assert result["messages"][-1].content == state["messages"][-1].content
    app_config.llm.context_window_tokens = 1048576
    with closing(sqlite3.connect(database, check_same_thread=False)) as connection:
        graph = _compression_graph(SqliteSaver(connection, serde=build_checkpoint_serializer()))
        restored = graph.get_state(config).values
        assert restored["tool_output_overrides"] == result["tool_output_overrides"]
        result2 = graph.invoke({"messages": [AIMessage(content="已读", id="answer"),
                                              HumanMessage(content="继续", id="next")]},
                               config=config, context=context)
        following = nodes._prompt_window(result2, context)
        assert not following.tool_result_overrides
        assert [m.model_dump() for m in following.messages[:len(sent.messages)]] == [
            m.model_dump() for m in sent.messages]
        assert len(result2["decision_trace"]) == len(result["decision_trace"])


def test_legacy_read_only_clips_with_pressure_and_never_fabricates_coordinates():
    raw = ReadFileResult(path="legacy.py", content="x = 1\n" * 2000, line_count=2000).model_dump_json()
    message = ToolMessage(content=raw, tool_call_id="old", name="read_file")
    roomy = select_prompt_window([message], enabled=True, max_messages=100,
                                 tool_result_keep_chars=8192, prune_tool_steps=False)
    data = json.loads(roomy.messages[0].content)
    assert data["content"] == json.loads(raw)["content"]
    assert data["source_coordinates"] == "unknown" and not data["truncated"]
    pressure = select_prompt_window([message], enabled=True, max_messages=100,
                                    tool_result_keep_chars=8192, prune_tool_steps=False,
                                    prune_tool_results=True)
    clipped = json.loads(pressure.messages[0].content)
    assert clipped["truncated"] and clipped["has_more"]
    assert clipped["next_start_line"] is None and clipped["source_coordinates"] == "unknown"
    assert "visible_ranges" not in clipped and len(clipped["content"]) < 8192
    assert message.content == raw
    disabled = select_prompt_window([message], enabled=False, max_messages=100,
                                    tool_output_overrides=pressure.tool_result_overrides)
    assert disabled.messages[0].content == raw


def test_reused_call_and_message_identity_with_different_result_does_not_replay_old_text():
    state = _state()
    message = state["messages"][-1]
    old = {tool_output_key(message): "old clipped result"}
    other = message.model_copy(update={"content": "a different verified file result"})
    window = select_prompt_window([other], enabled=True, max_messages=100,
                                  tool_output_overrides=old, prune_tool_steps=False)
    assert window.messages[-1].content == other.content


def test_current_task_compacts_only_complete_old_tool_prefix(tmp_path, app_config, model_factory):
    context = _context(tmp_path, app_config, model_factory, capacity=6000, keep=1)
    state = _state(6, body_size=3000)
    result = nodes.compression_node(state, Runtime(context=context))
    assert result["compression_count"] == 1
    assert result["context_budget"].summary_covered_messages == 11
    assert not result.get("tool_output_overrides")  # 每条正文未超单条阈值。
    updated = {**state, **result}
    remaining = nodes._prompt_window(updated, context)
    assert isinstance(remaining.messages[0], AIMessage)
    assert isinstance(remaining.messages[1], ToolMessage)
    assert remaining.messages[0].tool_calls[0]["id"] == remaining.messages[1].tool_call_id == "5"
    assert len(state["messages"]) == 13


def test_summary_failure_keeps_audit_and_does_not_advance_boundary(tmp_path, app_config, model_factory):
    context = _context(tmp_path, app_config, model_factory, capacity=6000, keep=1)
    context.model_adapter = model_factory(fail_summary=True)
    state = _state(6, body_size=3000)
    result = nodes.compression_node(state, Runtime(context=context))
    assert result["context_budget"].summary_covered_messages == 0
    assert "conversation_summary" not in result and "compression_count" not in result
    assert result["error_events"]
    assert len(nodes._prompt_window({**state, **result}, context).messages) == 13


def test_over_budget_with_nothing_to_compress_reports_a_visible_warning(
    tmp_path, app_config, model_factory,
):
    """触发压缩但没有可摘要的旧消息、且已超预算：必须留下**可见告警**，而不是静默照发。

    复审实证：设了很小的窗口、聊几轮就超，却一直没有压缩动作，页面上也没有任何信号。
    """
    context = _context(tmp_path, app_config, model_factory, capacity=6000)
    state = {
        "agent_loop_version": 1, "run_id": "r", "run_user_message_id": "r:user",
        "mode": "chat", "messages": [HumanMessage(content="很长的一句话" * 2000, id="r:user")],
    }

    result = nodes.compression_node(state, Runtime(context=context))

    assert result["context_budget"].usage_ratio >= 1.0
    assert "conversation_summary" not in result  # 没有更早的旧消息可摘要
    warnings = [
        event for event in result["error_events"] if event["stage"] == nodes.COMPRESSION_NODE
    ]
    assert len(warnings) == 1
    assert "无法再压缩" in warnings[0]["message"]
    # 同一轮重复进入（工具循环的每一步都会再进一次）不重复刷同一条告警。
    again = nodes.compression_node({**state, **result}, Runtime(context=context))
    assert not again.get("error_events")


@pytest.mark.parametrize("limit,expected_partial,expected_next", [(6, [], 2), (9, [2], 2)])
def test_read_tool_reports_character_cut_at_line_boundary_and_inside_line(
    tmp_path, app_config, model_factory, limit, expected_partial, expected_next,
):
    context = _context(tmp_path, app_config, model_factory)
    (tmp_path / "note.txt").write_bytes(b"first\nsecond\nthird\n")
    result = read_file_content("note.txt", policy=context.security_policy, max_chars=limit)
    assert result.partial_lines == expected_partial
    assert result.next_start_line == expected_next and result.has_more


def test_tiny_prune_threshold_never_expands_model_view():
    raw = ReadFileResult(path="tiny.py", content="xx", line_count=1,
                         first_line=1, last_line=1).model_dump_json()
    message = ToolMessage(content=raw, name="read_file", tool_call_id="tiny")
    full = select_prompt_window([message], enabled=True, max_messages=100,
                                prune_tool_steps=False)
    pressure = select_prompt_window([message], enabled=True, max_messages=100,
                                    tool_result_keep_chars=1, prune_tool_results=True,
                                    prune_tool_steps=False)
    assert pressure.messages[0].content == full.messages[0].content
    assert not pressure.tool_result_overrides
    assert not json.loads(pressure.messages[0].content)["truncated"]


@pytest.mark.parametrize("frozen", [False, True])
def test_pressure_summary_and_kept_steps_share_remaining_prompt_budget(tmp_path, app_config, model_factory, frozen):
    from local_agent.context.token_budget import estimate_tokens

    # 2026-10-29：`ask_user` 改成题组（复合 schema）+ 工具规范加了一条提问说明后，固定前缀
    # 多出约 300 tokens；这几条压力用例的模拟窗口随之上调 1000（断言\"压完必须 <1\"不变）。
    context = _context(tmp_path, app_config, model_factory, capacity=21000, keep=1)
    app_config.llm.max_tokens = 4096
    app_config.memory.compression.tool_step_budget_chars = 20000
    app_config.memory.compression.summary_max_chars = 12000
    class LongSummaryModel:
        def invoke(self, messages):
            return AIMessage(content="证据和未完成事项" * 1500)
    context.model_adapter = LongSummaryModel()
    state = _state(8, body_size=6000)
    if frozen:
        state["runtime_context_snapshots"] = [nodes._runtime_snapshot(state, context)]
    result = nodes.compression_node(state, Runtime(context=context))
    assert result["compression_count"] == 1
    assert estimate_tokens(result["conversation_summary"]) < app_config.memory.compression.summary_max_chars
    assert result["context_budget"].usage_ratio < 1
    assert len(state["messages"]) == 17
    remaining = nodes._prompt_window({**state, **result}, context)
    assert isinstance(remaining.messages[-1], ToolMessage)
    assert remaining.messages[-1].tool_call_id == "7"


def test_pressure_keeps_original_task_and_exact_completed_plan_even_if_model_summary_drifts(tmp_path, app_config, model_factory):
    from local_agent.graph.state import PlanStep

    # 2026-10-29：`ask_user` 改成题组（复合 schema）+ 工具规范加了一条提问说明后，固定前缀
    # 多出约 300 tokens；这几条压力用例的模拟窗口随之上调 1000（断言\"压完必须 <1\"不变）。
    context = _context(tmp_path, app_config, model_factory, capacity=21000, keep=1)
    app_config.llm.max_tokens = 4096
    state = _state(8, body_size=6000)
    state.update(task_goal="核对全部检索方法，只读不修改", plan=[
        PlanStep(id="r:1", description="核对自动检索", status="completed"),
        PlanStep(id="r:2", description="核对记忆检索", status="running"),
    ])
    state["runtime_context_snapshots"] = [nodes._runtime_snapshot(state, context)]
    class DriftingSummaryModel:
        def invoke(self, messages):
            return AIMessage(content="任务是只阅读 rag/search.py，已经读取片段。")
    context.model_adapter = DriftingSummaryModel()
    result = nodes.compression_node(state, Runtime(context=context))
    summary = result["conversation_summary"]
    assert summary.startswith("【本轮任务目标】\n核对全部检索方法，只读不修改")
    assert '"step":"核对自动检索","status":"completed"' in summary
    assert '"step":"核对记忆检索","status":"running"' in summary
    assert state["plan"][0].status == "completed"
    assert result["context_budget"].usage_ratio < 1
    app_config.memory.compression.summary_max_chars = 10
    limited = nodes.compression_node(state, Runtime(context=context))
    assert "compression_count" not in limited
    assert "conversation_summary" not in limited
    assert limited["context_budget"].summary_covered_messages == 0
    assert limited["error_events"]


def test_insufficient_evidence_room_does_not_destroy_summary_or_advance_coverage(
    tmp_path, app_config, model_factory, monkeypatch,
):
    from local_agent.graph.state import PlanStep

    # 2026-10-29：`ask_user` 改成题组（复合 schema）+ 工具规范加了一条提问说明后，固定前缀
    # 多出约 300 tokens；这几条压力用例的模拟窗口随之上调 1000（断言\"压完必须 <1\"不变）。
    context = _context(tmp_path, app_config, model_factory, capacity=21000, keep=1)
    app_config.llm.max_tokens = 4096
    state = _state(8, body_size=6000)
    state.update(task_goal="核对全部检索方法，只读不修改", plan=[
        PlanStep(id="r:1", description="已完成的模块与参数调查" * 10, status="completed"),
        PlanStep(id="r:2", description="剩余方法与引用" * 10, status="running"),
    ], conversation_summary="已确认 src/rag/search.py:12 使用固定词项检索，下一步核对记忆召回。")
    state["runtime_context_snapshots"] = [nodes._runtime_snapshot(state, context)]
    fixed = (15904 - nodes._stable_prompt(state, context).estimated_tokens
             - nodes._tool_definition_tokens(state, context) - 50)
    monkeypatch.setattr(nodes, "_snapshot_tokens", lambda *args, **kwargs: fixed)
    class MustNotSummarize:
        def invoke(self, messages):
            pytest.fail("insufficient evidence room must not purchase a destructive summary")
    context.model_adapter = MustNotSummarize()
    result = nodes.compression_node(state, Runtime(context=context))
    assert "conversation_summary" not in result
    assert "compression_count" not in result
    assert result["context_budget"].summary_covered_messages == 0
    assert result["error_events"]
    assert "src/rag/search.py:12" in state["conversation_summary"]


def test_rolling_summary_does_not_spend_evidence_budget_on_duplicate_protection():
    from local_agent.memory.summarizer import summarize_conversation

    header = '【本轮任务目标】\n只读调查\n【已记录显式计划，标题与状态须保留】\n[{"step":"核对检索","status":"running"}]\n'
    evidence = "src/rag/search.py:12 使用固定词项检索；待核对记忆召回。"
    class EchoProtectedModel:
        def invoke(self, messages):
            assert header not in messages[1].content
            assert evidence in messages[1].content
            assert "目标和计划由程序单独保留" in messages[0].content
            return AIMessage(content=header + evidence)
    result = summarize_conversation(EchoProtectedModel(), messages=[ToolMessage(
        content="新证据", tool_call_id="x", name="read_file")], previous_summary=header + evidence,
        max_chars=80)
    assert result == evidence


@pytest.mark.parametrize("plan", ['not JSON', '[{"step":"x","status":[]}]', '[{"step":"x","status":"completed","extra":"keep"}]'])
def test_malformed_summary_protection_is_retained_as_evidence(plan):
    from local_agent.memory.summarizer import summarize_conversation

    text = '【本轮任务目标】\n目标\n【已记录显式计划，标题与状态须保留】\n' + plan + '\n证据正文'
    class SummaryModel:
        def invoke(self, messages):
            return AIMessage(content=text)
    assert summarize_conversation(SummaryModel(), messages=[HumanMessage(content="x")],
                                  previous_summary=None, max_chars=1000) == text


def test_header_only_summary_is_a_failure_instead_of_an_empty_replacement():
    from local_agent.exceptions import LocalAgentError
    from local_agent.memory.summarizer import summarize_conversation

    header = '【本轮任务目标】\n目标\n【已记录显式计划，标题与状态须保留】\n[{"step":"调查","status":"pending"}]\n'
    class SummaryModel:
        def invoke(self, messages):
            return AIMessage(content=header)
    with pytest.raises(LocalAgentError, match="空内容"):
        summarize_conversation(SummaryModel(), messages=[HumanMessage(content="x")],
                               previous_summary=header + "原证据", max_chars=128)
