"""T66 真实隔离工具执行、单次提议、父审批与产物/测试双重校验。"""

from __future__ import annotations

import codecs
import json
from dataclasses import replace

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.types import Command
from pydantic import Field, ValidationError

from local_agent.cancellation import CancellationToken
from local_agent.config import WorkflowsConfig
from local_agent.graph import builder, code_workflow
from local_agent.llm.compat import ChatOpenAIWithReasoning
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.persistence import checkpoint
from local_agent.workflow_eval import (
    BROKEN_CODE,
    CODE_CASE,
    FIXED_CODE,
    TEST_CODE,
    _config,
    validate_outcome,
)


class ProposalModel(GenericFakeChatModel):
    body: str = json.dumps({"old_text": "    return value\n", "new_text": "    return min(upper, max(lower, value))\n"})
    requests: list[dict] = Field(default_factory=list)
    raises: bool = False
    usage: dict | None = None
    max_retries: int = 2

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append({"messages": messages, "max_retries": self.max_retries, **kwargs})
        if self.raises:
            raise RuntimeError("model failed")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.body, usage_metadata=self.usage))])


def _environment(tmp_path, *, body=None, limits=None, request=None, raises=False, usage=None):
    for path, content in CODE_CASE.seeds:
        (tmp_path / path).write_bytes(content.encode())
    config = _config(tmp_path, "coder")
    config.workflows = WorkflowsConfig(enabled=True, **(limits or {}))
    token = CancellationToken()
    model = ProposalModel(messages=iter([]), raises=raises, usage=usage, **({"body": body} if body is not None else {}))
    recorder = ModelCallRecorder(connection_id="offline", config_version="T66", provider="scripted",
        model_id="scripted-offline", endpoint_fingerprint="offline", context_window_tokens=config.require_llm().context_window_tokens)
    context = builder.build_runtime_context(app_config=config.as_run_config(), model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=token, usage_recorder=recorder,
        code_change=request if request is not None else {
            "source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3,
        })
    return context, model


def _invoke(context, graph=None, config=None):
    return (graph or builder.build_chat_graph()).invoke({"user_input": CODE_CASE.prompt, "session_mode": "agent"},
                                                      context=context, config=config)


def test_code_subgraph_runs_fixed_task_real_tests_and_records_once(tmp_path):
    context, model = _environment(tmp_path)
    graph = builder.build_chat_graph()
    assert "code_change" in dict(graph.get_subgraphs())
    result = _invoke(context, graph)
    assert result["task_status"] == "completed", result["final_response"]
    assert validate_outcome(CODE_CASE, tmp_path, result) == []
    assert len(model.requests) == 1
    assert model.requests[0]["max_retries"] == 0
    assert model.max_retries == 2
    assert model.requests[0]["max_completion_tokens"] == 2048
    assert result["workflow"]["model_calls"] == 1
    assert 0 < result["workflow"]["model_tokens_reserved"] <= 16000
    assert result["workflow"]["tool_calls"] == 6
    assert result["workflow"]["steps"] == 16
    assert len(result["model_call_records"]) == 1
    record = result["model_call_records"][0]
    assert record.actual_output_tokens is None
    assert result["turn_records"][result["run_id"]].model_call_ids == (record.model_call_id,)
    assert len(result["final_evidence"]) == 3
    assert result["reflection_result"].evidence == tuple(result["final_evidence"])


@pytest.mark.parametrize("case_input", [
    {}, {"source_path": "../bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3},
    {"source_path": "/bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3},
    {"source_path": "bounds.py", "test_paths": ["bounds.py"], "expected_test_count": 3},
    {"source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 0},
    {"source_path": "bounds.py", "test_paths": [], "expected_test_count": 3},
    {"source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3, "test_command": "python -c pass"},
    {"source_path": "./bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3},
    {"source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3, "next_node": "write"},
])
def test_invalid_request_has_no_side_effects(tmp_path, case_input):
    context, model = _environment(tmp_path, request=case_input)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert model.requests == []
    assert result.get("tool_events", []) == []
    assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE


@pytest.mark.parametrize("limits", [
    {"max_tool_calls": 1}, {"max_steps": 1}, {"max_source_files": 1},
    {"max_evidence_chars": 1}, {"max_report_chars": 1}, {"max_model_tokens": 1},
    {"max_model_output_tokens": 1},
])
def test_budget_exhaustion_is_failure(tmp_path, limits):
    context, model = _environment(tmp_path, limits=limits)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    data = result["workflow"]
    assert data["steps"] <= context.app_config.workflows.max_steps
    assert data["tool_calls"] <= context.app_config.workflows.max_tool_calls
    assert data["model_tokens_reserved"] <= context.app_config.workflows.max_model_tokens
    if limits.get("max_model_tokens") == 1:
        assert model.requests == []


@pytest.mark.parametrize("body", [
    "已经修好了", "{}", '{"old_text":"return value","new_text":"return value"}',
    '{"old_text":"missing","new_text":"x"}',
    '{"old_text":"return value","new_text":"return 5","path":"test_bounds.py"}',
])
def test_invalid_model_proposal_never_writes(tmp_path, body):
    context, model = _environment(tmp_path, body=body)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert len(model.requests) == 1
    assert not any(event.tool_name == "edit_file" for event in result["tool_events"])
    assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE


def test_model_exception_is_recorded_without_retry(tmp_path):
    context, model = _environment(tmp_path, raises=True)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert len(model.requests) == 1
    assert len(result["model_call_records"]) == 1
    assert result["model_call_records"][0].status == "error"
    assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE


def test_actual_usage_over_reservation_stops_before_edit(tmp_path):
    context, model = _environment(tmp_path, usage={"input_tokens": 20000, "output_tokens": 10, "total_tokens": 20010})
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert "预留" in result["final_response"]
    assert len(model.requests) == 1
    assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE


def test_real_failed_tests_do_not_complete_or_rollback(tmp_path):
    body = json.dumps({"old_text": "    return value\n", "new_text": "    return 5\n"})
    context, _ = _environment(tmp_path, body=body)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert "测试失败" in result["final_response"]
    assert (tmp_path / "bounds.py").read_text().endswith("    return 5\n")
    assert (tmp_path / "test_bounds.py").read_text() == TEST_CODE
    assert sum(event.tool_name == "edit_file" for event in result["tool_events"]) == 1


def test_expected_test_count_is_checked(tmp_path):
    context, _ = _environment(tmp_path, request={"source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 4})
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert "数量不符" in result["final_response"]


@pytest.mark.parametrize("when", ["before", "after_read", "after_edit", "after_test", "before_report"])
def test_cancellation_stops_future_actions(tmp_path, monkeypatch, when):
    context, model = _environment(tmp_path)
    original = code_workflow.execute
    if when == "before":
        context.cancellation.cancel("test")
    def execute(state, runtime):
        update = original(state, runtime)
        name = state["pending_tool_calls"][0]["name"]
        count = state["workflow"]["tool_calls"]
        if ((when == "after_read" and count == 2) or (when == "after_edit" and name == "edit_file")
                or (when == "after_test" and name == "run_shell") or (when == "before_report" and count == 6)):
            context.cancellation.cancel("test")
        return update
    monkeypatch.setattr(code_workflow, "execute", execute)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    if when in {"before", "after_read"}:
        assert model.requests == []
        assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE
    if when == "after_edit":
        assert not any(event.tool_name == "run_shell" for event in result["tool_events"])


def test_file_version_change_before_edit_is_rejected(tmp_path, monkeypatch):
    context, _ = _environment(tmp_path)
    original = code_workflow.propose
    def propose(state, runtime):
        update = original(state, runtime)
        (tmp_path / "bounds.py").write_text("# user edit\n" + BROKEN_CODE)
        return update
    monkeypatch.setattr(code_workflow, "propose", propose)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert (tmp_path / "bounds.py").read_text() == "# user edit\n" + BROKEN_CODE


@pytest.mark.parametrize("target", ["bounds.py", "test_bounds.py"])
def test_post_test_changes_fail_reread_validation(tmp_path, monkeypatch, target):
    context, _ = _environment(tmp_path)
    original = code_workflow.execute
    def execute(state, runtime):
        update = original(state, runtime)
        if state["pending_tool_calls"][0]["name"] == "run_shell":
            path = tmp_path / target
            path.write_bytes(path.read_bytes() + b"\n# changed\n")
        return update
    monkeypatch.setattr(code_workflow, "execute", execute)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert "复读" in result["final_response"]


@pytest.mark.parametrize("decision", ["approve", "reject", "edit"])
def test_write_approval_uses_parent_node_and_same_call(tmp_path, decision):
    context, model = _environment(tmp_path)
    context.app_config.security.large_edit.max_changed_lines_without_approval = 1
    saver = checkpoint.open_checkpointer(tmp_path / "checkpoints.sqlite")
    try:
        graph = builder.build_chat_graph(saver)
        cfg = {"configurable": {"thread_id": "write-approval"}}
        pending = _invoke(context, graph, cfg)
        assert pending["workflow"]["phase"] == "waiting"
        assert pending["pending_approval"]["tool_name"] == "edit_file"
        assert graph.get_state(cfg).next == ("approval_interrupt_node",)
        assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE
        result = graph.invoke(Command(resume={"decision": decision}), config=cfg, context=context)
        assert len(model.requests) == 1
        assert len(result["model_call_records"]) == 1
        if decision == "approve":
            assert result["task_status"] == "completed", result["final_response"]
            assert (tmp_path / "bounds.py").read_text() == FIXED_CODE
            assert sum(event.tool_name == "edit_file" for event in result["tool_events"]) == 1
        else:
            assert result["task_status"] == "failed"
            assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE
    finally:
        saver.conn.close()


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_shell_approval_does_not_repeat_write(tmp_path, decision):
    context, model = _environment(tmp_path)
    context.app_config.security.shell.whitelist = []
    saver = checkpoint.open_checkpointer(tmp_path / "checkpoints.sqlite")
    try:
        graph = builder.build_chat_graph(saver)
        cfg = {"configurable": {"thread_id": "shell-approval"}}
        pending = _invoke(context, graph, cfg)
        assert pending["pending_approval"]["tool_name"] == "run_shell"
        assert (tmp_path / "bounds.py").read_text() == FIXED_CODE
        result = graph.invoke(Command(resume={"decision": decision}), config=cfg, context=context)
        assert sum(event.tool_name == "edit_file" for event in result["tool_events"]) == 1
        assert len(model.requests) == len(result["model_call_records"]) == 1
        assert result["task_status"] == ("completed" if decision == "approve" else "failed")
    finally:
        saver.conn.close()


@pytest.mark.parametrize("kind", ["disabled", "chat", "capabilities", "two_workflows"])
def test_unavailable_workflow_never_invokes_model(tmp_path, kind):
    context, model = _environment(tmp_path)
    if kind == "disabled":
        context.app_config.workflows.enabled = False
    elif kind == "capabilities":
        context.agent_capabilities = ("files_read", "shell")
    elif kind == "two_workflows":
        context.readonly_research = {"pattern": "*.py"}
    result = builder.build_chat_graph().invoke({"user_input": CODE_CASE.prompt, "session_mode": "chat" if kind == "chat" else "agent"}, context=context)
    assert result["task_status"] == "failed"
    assert model.requests == []


def test_bom_and_crlf_survive_edit_and_real_test(tmp_path):
    body = json.dumps({"old_text": "    return value\r\n", "new_text": "    return min(upper, max(lower, value))\r\n"})
    context, _ = _environment(tmp_path, body=body)
    (tmp_path / "bounds.py").write_bytes(codecs.BOM_UTF8 + BROKEN_CODE.replace("\n", "\r\n").encode())
    result = _invoke(context)
    assert result["task_status"] == "completed", result["final_response"]
    assert (tmp_path / "bounds.py").read_bytes() == codecs.BOM_UTF8 + FIXED_CODE.replace("\n", "\r\n").encode()


@pytest.mark.parametrize("field", ["max_model_tokens", "max_model_output_tokens"])
def test_nonpositive_model_budget_is_invalid(field):
    with pytest.raises(ValidationError):
        WorkflowsConfig(**{field: 0})


def test_test_file_case_alias_cannot_be_edit_target(tmp_path):
    context, model = _environment(tmp_path, request={"source_path": "bounds.py", "test_paths": ["BOUNDS.py"], "expected_test_count": 3})
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert model.requests == []


@pytest.mark.parametrize("fault", [
    "missing_body", "old_run", "truncated", "timed_out", "exit_code", "empty_output", "skipped",
    "bad_edit_hash", "bad_read_hash",
])
def test_incomplete_or_inconsistent_tool_evidence_is_rejected(tmp_path, monkeypatch, fault):
    context, _ = _environment(tmp_path)
    original = code_workflow.execute
    target = "edit_file" if fault == "bad_edit_hash" else "read_file" if fault == "bad_read_hash" else "run_shell"
    def execute(state, runtime):
        update = original(state, runtime)
        if state["pending_tool_calls"][0]["name"] != target:
            return update
        if fault == "missing_body":
            update["messages"] = []
        elif fault == "old_run":
            update["tool_events"] = [replace(event, run_id="old-run") for event in update["tool_events"]]
        else:
            messages = []
            for message in update["messages"]:
                if isinstance(message, ToolMessage):
                    payload = json.loads(message.content)
                    if fault in {"truncated", "timed_out"}:
                        payload[fault] = True
                    elif fault == "exit_code":
                        payload["exit_code"] = 1
                    elif fault == "empty_output":
                        payload["stderr"] = ""
                    elif fault == "skipped":
                        payload["stderr"] = "Ran 3 tests in 0.001s\n\nOK (skipped=1)\n"
                    elif fault == "bad_edit_hash":
                        payload["new_sha256"] = "0" * 64
                    else:
                        payload["sha256"] = "0" * 64
                    message = message.model_copy(update={"content": json.dumps(payload)})
                messages.append(message)
            update["messages"] = messages
        return update
    monkeypatch.setattr(code_workflow, "execute", execute)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert result["final_evidence"] == []


def test_two_approvals_preserve_counts_and_do_not_repeat_side_effects(tmp_path):
    context, model = _environment(tmp_path)
    context.app_config.security.large_edit.max_changed_lines_without_approval = 1
    context.app_config.security.shell.whitelist = []
    saver = checkpoint.open_checkpointer(tmp_path / "checkpoints.sqlite")
    try:
        graph = builder.build_chat_graph(saver)
        cfg = {"configurable": {"thread_id": "both"}}
        first = _invoke(context, graph, cfg)
        assert first["pending_approval"]["tool_name"] == "edit_file"
        second = graph.invoke(Command(resume={"decision": "approve"}), config=cfg, context=context)
        assert second["pending_approval"]["tool_name"] == "run_shell"
        result = graph.invoke(Command(resume={"decision": "approve"}), config=cfg, context=context)
        assert result["task_status"] == "completed", result["final_response"]
        assert result["workflow"]["tool_calls"] == 6
        assert len(result["tool_events"]) == len({event.tool_call_id for event in result["tool_events"]}) == 6
        assert len(model.requests) == len(result["model_call_records"]) == 1
        assert result["workflow"]["steps"] == 16
    finally:
        saver.conn.close()


def test_cancel_while_waiting_for_write_approval_does_not_execute(tmp_path):
    context, model = _environment(tmp_path)
    context.app_config.security.large_edit.max_changed_lines_without_approval = 1
    saver = checkpoint.open_checkpointer(tmp_path / "checkpoints.sqlite")
    try:
        graph = builder.build_chat_graph(saver)
        cfg = {"configurable": {"thread_id": "cancel-pending"}}
        pending = _invoke(context, graph, cfg)
        assert pending["pending_approval"]
        context.cancellation.cancel("test")
        result = graph.invoke(Command(resume={"decision": "approve"}), config=cfg, context=context)
        assert result["task_status"] == "failed"
        assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE
        assert len(model.requests) == 1
        assert not any(event.tool_name == "edit_file" for event in result["tool_events"])
    finally:
        saver.conn.close()


def test_next_unselected_turn_uses_free_loop_without_duplicate_workflow_records(tmp_path, model_factory):
    context, model = _environment(tmp_path)
    saver = checkpoint.open_checkpointer(tmp_path / "checkpoints.sqlite")
    try:
        graph = builder.build_chat_graph(saver)
        cfg = {"configurable": {"thread_id": "normal-after"}}
        first = _invoke(context, graph, cfg)
        assert first["task_status"] == "completed"
        normal = model_factory(reply="普通回答")
        context = builder.build_runtime_context(app_config=context.app_config, model_adapter=normal,
            workspace_root=tmp_path, current_cwd=tmp_path, usage_recorder=context.usage_recorder)
        result = graph.invoke({"user_input": "你好", "session_mode": "chat"}, config=cfg, context=context)
        assert result["workflow"] is None
        assert result["final_response"] == "普通回答"
        assert len(result["tool_events"]) == 6
        assert len(model.requests) == 1
        identities = [record.model_call_id for record in result["model_call_records"]]
        assert len(identities) == len(set(identities))
        assert identities.count(first["model_call_records"][0].model_call_id) == 1
    finally:
        saver.conn.close()


def test_real_sdk_failure_does_not_retry_or_bypass_single_call_budget(tmp_path):
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(503, json={"error": {"message": "offline failure", "type": "server_error"}})
    context, _ = _environment(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAIWithReasoning(model="deepseek-flash", api_key="offline-unused",
            base_url="http://127.0.0.1:1/v1", http_client=client, max_retries=2, max_completion_tokens=4096)
        context.model_adapter = model
        result = _invoke(context)
        assert result["task_status"] == "failed"
        assert model.root_client.max_retries == model.max_retries == 2
        assert len(captured) == 1
        assert captured[0]["max_tokens"] == 2048
        assert len(result["model_call_records"]) == 1
        assert (tmp_path / "bounds.py").read_text() == BROKEN_CODE
