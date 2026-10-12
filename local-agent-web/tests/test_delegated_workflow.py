"""T67 真工具、上下文/目录隔离、并发总额度、超时及原证据核对。"""

from __future__ import annotations

import codecs
import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from threading import Barrier
from typing import Any

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, ValidationError

from local_agent.application.projection import project_usage
from local_agent.cancellation import CancellationToken
from local_agent.config import WorkflowsConfig
from local_agent.exceptions import SecurityError
from local_agent.graph import builder, delegated_workflow, nodes, readonly_workflow
from local_agent.llm.compat import ChatOpenAIWithReasoning
from local_agent.llm.usage_recording import ModelCallRecorder
from local_agent.memory.schemas import is_auto_injectable
from local_agent.persistence import migrations
from local_agent.persistence.checkpoint import open_checkpointer
from local_agent.persistence.job_store import JobStore
from local_agent.persistence.session_store import SessionStore
from local_agent.security.path_guard import ExternalPathPolicy
from local_agent.workflow_contracts import DelegatedResearchRequest
from local_agent.workflow_eval import READ_CASE, _config


class ResearchModel(GenericFakeChatModel):
    body: str = json.dumps({"pattern": "*.md", "keywords": []})
    requests: list[dict] = Field(default_factory=list)
    max_retries: int = 2
    delay: float = 0
    raises: bool = False
    gate: Any = None
    usage: dict | None = None
    tool_call: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append({"messages": messages, "max_retries": self.max_retries, **kwargs})
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("private error must not leak")
        calls = [{"name": "run_shell", "args": {"command": "evil"}, "id": "evil"}] if self.tool_call else []
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=self.body, tool_calls=calls, usage_metadata=self.usage))])


def _environment(tmp_path, *, count=1, concurrency=1, limits=None, body=None, model_options=None):
    for path, content in READ_CASE.seeds:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())
    tasks = [{"scope": "docs", "question": "核对取消、恢复与权限原文"}]
    if count > 1:
        tasks = []
        for index in range(count):
            scope = f"part{index}"
            target = tmp_path / scope / "fact.md"
            target.parent.mkdir()
            target.write_bytes(f"# 原文\n独立证据 {index}\n".encode())
            tasks.append({"scope": scope, "question": f"核对独立证据 {index}"})
    config = _config(tmp_path, "coder")
    config.workflows = WorkflowsConfig(enabled=True, **(limits or {}))
    model = ResearchModel(messages=iter([]), **({"body": body} if body is not None else {}), **(model_options or {}))
    recorder = ModelCallRecorder(connection_id="offline", config_version="T67", provider="scripted",
        model_id="scripted-offline", endpoint_fingerprint="offline",
        context_window_tokens=config.require_llm().context_window_tokens)
    context = builder.build_runtime_context(app_config=config.as_run_config(), model_adapter=model,
        workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(), usage_recorder=recorder,
        delegated_research={"tasks": tasks, "concurrency": concurrency})
    return context, model


def _invoke(context, **extra):
    return builder.build_chat_graph().invoke({"user_input": "核对原文", "session_mode": "agent", **extra}, context=context)


@pytest.mark.parametrize("count,concurrency", [(1, 1), (2, 1), (2, 2), (3, 1), (3, 2)])
def test_real_readonly_children_and_total_usage(tmp_path, count, concurrency):
    context, model = _environment(tmp_path, count=count, concurrency=concurrency)
    before = {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.rglob("*") if path.is_file()}
    result = _invoke(context)
    assert result["task_status"] == "completed", result["final_response"]
    data = result["workflow"]
    assert len(data["children"]) == count
    assert len(model.requests) == data["model_calls"] == count
    assert model.max_retries == 2 and all(call["max_retries"] == 0 for call in model.requests)
    assert all(call["max_completion_tokens"] == 2048 for call in model.requests)
    assert data["tool_calls"] == (4 if count == 1 else 2 * count)
    assert data["steps"] <= context.app_config.workflows.max_steps
    assert data["model_tokens_reserved"] <= context.app_config.workflows.max_model_tokens
    assert all(event.status == "ok" and event.tool_name in {"glob", "read_file"} for event in result["tool_events"])
    assert all(event.run_id == result["run_id"] for event in result["tool_events"])
    assert len(result["model_call_records"]) == count
    records = result["model_call_records"]
    assert len({record.model_call_id for record in records}) == count
    assert all(record.run_id == result["run_id"] and record.actual_output_tokens is None for record in records)
    assert set(result["turn_records"][result["run_id"]].model_call_ids) == {record.model_call_id for record in records}
    usage = project_usage(result)
    assert usage is not None and set(usage.model_call_ids) == {record.model_call_id for record in records}
    assert before == {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in tmp_path.rglob("*") if path.is_file()}
    assert all(evidence.verified for evidence in result["final_evidence"])
    assert not any(isinstance(message, ToolMessage) for message in result["messages"])


def test_parallel_is_real_and_shares_the_serial_budget(tmp_path):
    context, model = _environment(tmp_path, count=2, concurrency=2, model_options={"gate": Barrier(2)})
    result = _invoke(context)
    assert result["task_status"] == "completed", result["final_response"]
    reserved = result["workflow"]["model_tokens_reserved"]
    model.gate = None
    context.delegated_research["concurrency"] = 1
    serial = _invoke(context)
    assert serial["task_status"] == "completed"
    assert serial["workflow"]["model_tokens_reserved"] == reserved
    assert serial["workflow"]["tool_calls"] == result["workflow"]["tool_calls"]
    assert len(serial["model_call_records"]) == 2  # 本轮仅新子调用，不重复历史。


@pytest.mark.parametrize("case_input", [
    {}, {"tasks": []}, {"tasks": [{"scope": "../docs", "question": "q"}]},
    {"tasks": [{"scope": "/docs", "question": "q"}]},
    {"tasks": [{"scope": "E:/docs", "question": "q"}]},
    {"tasks": [{"scope": "docs\\x", "question": "q"}]},
    {"tasks": [{"scope": "./docs", "question": "q"}]},
    {"tasks": [{"scope": "docs", "question": " "}]},
    {"tasks": [{"scope": "docs", "question": "q", "tasks": []}]},
    {"tasks": [{"scope": "docs", "question": "q"}], "concurrency": 3},
    {"tasks": [{"scope": "docs", "question": "q"}], "concurrency": True},
    {"tasks": [{"scope": "docs", "question": "q"}], "next_node": "shell"},
])
def test_invalid_request_has_no_model_or_tool(tmp_path, case_input):
    context, model = _environment(tmp_path)
    context.delegated_research = case_input
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert not model.requests and not result.get("tool_events")


@pytest.mark.parametrize("setting", ["disabled", "chat", "capability", "cancelled", "conflict", "missing_scope", "file_scope"])
def test_preconditions_fail_before_calls(tmp_path, setting):
    context, model = _environment(tmp_path)
    extra = {}
    if setting == "disabled":
        context.app_config.workflows.enabled = False
    elif setting == "chat":
        extra["session_mode"] = "chat"
    elif setting == "capability":
        context.agent_capabilities = ("files_read",)
    elif setting == "cancelled":
        context.cancellation.cancel("test")
    elif setting == "conflict":
        context.code_change = {}
    else:
        context.delegated_research["tasks"][0]["scope"] = "missing" if setting == "missing_scope" else "docs/cancel.md"
    result = _invoke(context, **extra)
    assert result["task_status"] == "failed"
    assert not model.requests and not result.get("tool_events")


@pytest.mark.parametrize("limits,count,concurrency", [
    ({"max_steps": 1}, 1, 1), ({"max_model_tokens": 1}, 1, 1),
    ({"max_children": 1}, 2, 1), ({"max_concurrency": 1}, 2, 2),
    ({"max_tool_calls": 1}, 1, 1), ({"max_source_files": 1}, 1, 1),
    ({"max_evidence_chars": 1}, 1, 1), ({"max_report_chars": 1}, 1, 1),
    ({"max_tool_calls": 3}, 2, 1), ({"max_tool_calls": 3}, 2, 2),
    ({"max_steps": 10}, 2, 1), ({"max_steps": 10}, 2, 2),
])
def test_hard_total_limits_never_succeed(tmp_path, limits, count, concurrency):
    context, _ = _environment(tmp_path, count=count, concurrency=concurrency, limits=limits)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    data = result["workflow"]
    assert data["tool_calls"] <= context.app_config.workflows.max_tool_calls
    assert data["steps"] <= context.app_config.workflows.max_steps
    assert data["model_tokens_reserved"] <= context.app_config.workflows.max_model_tokens
    assert len(result["final_response"]) <= context.app_config.workflows.max_report_chars


@pytest.mark.parametrize("body", [
    "not json", json.dumps({"pattern": "../*.md"}), json.dumps({"pattern": "/etc/*"}),
    json.dumps({"pattern": "*.md", "tool": "write_file"}),
    json.dumps({"pattern": "missing/*.md"}), json.dumps({"pattern": "*.md", "keywords": ["不存在"]}),
])
def test_invalid_plan_or_missing_evidence_is_failure(tmp_path, body):
    context, _ = _environment(tmp_path, body=body)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert not result["final_evidence"]


@pytest.mark.parametrize("options", [{"raises": True}, {"tool_call": True},
    {"usage": {"input_tokens": 99999, "output_tokens": 1, "total_tokens": 100000}},
    {"usage": {"input_tokens": 1, "output_tokens": 99999, "total_tokens": 100000}}])
def test_model_failure_does_not_start_tools_and_keeps_usage(tmp_path, options):
    context, _ = _environment(tmp_path, model_options=options)
    result = _invoke(context)
    assert result["task_status"] == "failed" and not result.get("tool_events")
    assert len(result["model_call_records"]) == 1
    assert "private error" not in result["final_response"]


def test_timeout_stops_before_any_tool_and_notifies_parent(tmp_path):
    context, _ = _environment(tmp_path, limits={"child_timeout_seconds": 1}, model_options={"delay": 1.15})
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert "超时" in result["final_response"]
    assert not result.get("tool_events") and len(result["model_call_records"]) == 1


def test_parent_cancellation_during_child_execution(tmp_path, monkeypatch):
    context, _ = _environment(tmp_path)
    original = readonly_workflow.observe
    def cancel_after_read(state, runtime):
        update = original(state, runtime)
        if len(state.get("tool_events", [])) == 2:
            context.cancellation.cancel("after-read")
        return update
    monkeypatch.setattr(readonly_workflow, "observe", cancel_after_read)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert len([event for event in result["tool_events"] if event.status == "ok"]) == 2
    assert not result["final_evidence"]


def test_child_context_has_no_parent_history_or_authorization(tmp_path, monkeypatch):
    context, model = _environment(tmp_path)
    context.agents_context = "PRIVATE PARENT RULES"
    context.security_policy.external_policy = ExternalPathPolicy(mode="allow")
    seen = []
    original = readonly_workflow.initialize
    def capture(state, runtime):
        seen.append((dict(state), runtime.context))
        return original(state, runtime)
    monkeypatch.setattr(readonly_workflow, "initialize", capture)
    result = _invoke(context, messages=[HumanMessage(content="PRIVATE PARENT HISTORY")])
    assert result["task_status"] == "completed"
    child_state, child = seen[0]
    assert child_state["session_kind"] == "subagent"
    assert child_state["messages"] == [] and not child_state.get("approval_history")
    assert child.agents_context is None and child.delegated_research is None and child.code_change is None
    assert child.workspace_root == tmp_path / "docs" and child.current_cwd == child.workspace_root
    assert child.agent_capabilities == ("files_read", "code_search")
    assert child.security_policy is not context.security_policy and child.tool_registry is not context.tool_registry
    assert child.security_policy.external_policy.mode == "deny"
    assert child.security_policy.allow_result_spill is False
    assert not child.app_config.memory.long_term.enabled and not child.app_config.agents.enabled
    assert "PRIVATE" not in str(model.requests)
    assert nodes._candidate_origin(child_state) == "system"
    assert not is_auto_injectable("system")


@pytest.mark.parametrize("attack", ["write", "shell", "outside", "sensitive"])
def test_execution_gate_rejects_injected_child_tool_even_with_safety_bypassed(tmp_path, monkeypatch, attack):
    context, _ = _environment(tmp_path)
    context.security_policy.external_policy = ExternalPathPolicy(mode="allow")
    outside = tmp_path / "private.txt"
    outside.write_text("parent-only", encoding="utf-8")
    (tmp_path / "docs" / ".env").write_text("secret", encoding="utf-8")
    original = readonly_workflow.prepare_call
    def inject(state, runtime):
        update = original(state, runtime)
        call = update["pending_tool_calls"][0]
        if attack == "write":
            call.update(name="write_file", args={"path": "injected.txt", "content": "evil"})
        elif attack == "shell":
            call.update(name="run_shell", args={"command": "echo evil"})
        else:
            call.update(name="read_file", args={"path": str(outside) if attack == "outside" else ".env"})
        update["messages"] = [AIMessage(content="", tool_calls=[call])]
        return update
    monkeypatch.setattr(readonly_workflow, "prepare_call", inject)
    monkeypatch.setattr(readonly_workflow, "readonly_safety", lambda state, runtime: {})
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert result["tool_events"] and all(event.status == "denied" for event in result["tool_events"])
    assert outside.read_text() == "parent-only" and not (tmp_path / "docs" / "injected.txt").exists()


@pytest.mark.parametrize("corrupt", ["message", "event", "line", "hash", "file", "failed_state", "child_id"])
def test_parent_rejects_tampered_child_proof(tmp_path, monkeypatch, corrupt):
    context, _ = _environment(tmp_path)
    original = delegated_workflow._run_child
    def tamper(parent, item):
        result = original(parent, item)
        assert result.report["status"] == "completed"
        if corrupt == "message":
            result.state["messages"] = [message for message in result.state["messages"] if not isinstance(message, ToolMessage)]
        elif corrupt == "event":
            result.state["tool_events"] = [replace(event, run_id="other-child") for event in result.state["tool_events"]]
        elif corrupt == "line":
            result.state["workflow"]["references"][0]["line"] = 999
        elif corrupt == "hash":
            result.state["workflow"]["references"][0]["sha256"] = "0" * 64
        elif corrupt == "failed_state":
            result.state["task_status"] = "failed"
        elif corrupt == "child_id":
            result.report["child_id"] = "other-child"
        else:
            (tmp_path / "docs" / "cancel.md").write_text("changed", encoding="utf-8")
        return result
    monkeypatch.setattr(delegated_workflow, "_run_child", tamper)
    result = _invoke(context)
    assert result["task_status"] == "failed" and "父证据核对失败" in result["final_response"]
    assert not result["final_evidence"]


def test_child_graph_exception_keeps_completed_tool_audit(tmp_path, monkeypatch):
    context, _ = _environment(tmp_path)
    original = readonly_workflow.observe
    def broken(state, runtime):
        if len(state.get("tool_events", [])) == 2:
            raise RuntimeError("broken observer")
        return original(state, runtime)
    monkeypatch.setattr(readonly_workflow, "observe", broken)
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert len(result["tool_events"]) == 2 and len(result["model_call_records"]) == 1


def test_bom_crlf_evidence_is_verified_without_changing_file(tmp_path):
    context, _ = _environment(tmp_path)
    file = tmp_path / "docs" / "cancel.md"
    file.write_bytes(codecs.BOM_UTF8 + b"# Cancel\r\nCancellation keeps side effects.\r\n")
    before = file.read_bytes()
    result = _invoke(context)
    assert result["task_status"] == "completed", result["final_response"]
    assert file.read_bytes() == before


def test_sdk_single_request_and_timeout_with_mock_transport(tmp_path):
    context, _ = _environment(tmp_path, limits={"child_timeout_seconds": 1})
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "offline"}})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        model = ChatOpenAIWithReasoning(model="deepseek-chat", api_key="offline", base_url="http://127.0.0.1:1/v1",
                                      http_client=client, max_retries=2)
        context.model_adapter = model
        result = _invoke(context)
        assert result["task_status"] == "failed" and len(requests) == 1
        assert json.loads(requests[0].content)["max_tokens"] == 2048
        assert requests[0].extensions["timeout"]["read"] <= 1
        assert model.root_client.max_retries == 2
        assert len(result["model_call_records"]) == 1 and not result.get("tool_events")
    finally:
        client.close()


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_v9_preserves_parent_rows_foreign_keys_indexes_and_is_idempotent(tmp_path, foreign_keys):
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    try:
        connection.execute(f"PRAGMA foreign_keys={foreign_keys}")
        assert migrations._apply(connection, migrations._CHAT_MIGRATIONS[:8]) == 8
        store = SessionStore(connection, workspace_root=tmp_path)
        session = store.create()
        job = JobStore(connection).create(session_id=session.session_id)
        connection.execute("INSERT INTO conversation_branches VALUES (?, 'b', 'old', NULL, NULL, NULL, 0, 1)",
                           (session.session_id,))
        connection.commit()
        assert migrations.apply_migrations(connection) == migrations.apply_migrations(connection) == migrations.CHAT_SCHEMA_VERSION
        assert store.get(session.session_id) == session
        assert JobStore(connection).get(job.job_id) == job
        assert connection.execute("SELECT session_id FROM conversation_branches").fetchone()[0] == session.session_id
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == foreign_keys
        assert store.create(kind="subagent").kind == "subagent"
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"idx_sessions_status", "idx_sessions_updated_at", "idx_sessions_single_active", "idx_sessions_workspace"} <= indexes
    finally:
        connection.close()


def test_v9_failure_rolls_back_schema_version_and_restores_foreign_key_setting(tmp_path):
    connection = sqlite3.connect(tmp_path / "chat.sqlite")
    try:
        migrations._apply(connection, migrations._CHAT_MIGRATIONS[:8])
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO jobs VALUES ('orphan', 'missing', 'queued', NULL, 1, '', NULL, '', NULL, NULL)")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            migrations.apply_migrations(connection)
        assert migrations.current_version(connection) == 8
        assert "sessions_v9" not in migrations.table_names(connection)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO sessions VALUES ('s','t','x','x','x','','',NULL,'idle','agent',0,'subagent')")
    finally:
        connection.close()


def test_config_and_request_constraints_are_actual_consumers():
    for field, value in (("max_children", 4), ("max_concurrency", 3), ("child_timeout_seconds", 0)):
        with pytest.raises(ValidationError):
            WorkflowsConfig(**{field: value})
    with pytest.raises(ValidationError):
        DelegatedResearchRequest.model_validate({"tasks": [{"scope": ".", "question": "q"}] * 4})


def test_one_failed_child_never_upgrades_partial_success(tmp_path, monkeypatch):
    context, _ = _environment(tmp_path, count=2, concurrency=2)
    original = delegated_workflow._run_child
    def fail_one(parent, item):
        result = original(parent, item)
        if item.task.scope == "part1":
            result.report.update(status="failed", error="没有证据", references=[])
        return result
    monkeypatch.setattr(delegated_workflow, "_run_child", fail_one)
    result = _invoke(context)
    assert result["task_status"] == "failed" and not result["final_evidence"]
    assert [child["status"] for child in result["workflow"]["children"]] == ["completed", "failed"]
    assert len(result["model_call_records"]) == 2 and len(result["tool_events"]) == 4


def test_timeout_after_real_read_stops_next_tool_and_keeps_proof(tmp_path, monkeypatch):
    context, _ = _environment(tmp_path, limits={"child_timeout_seconds": 1})
    original = readonly_workflow.observe
    def slow_observe(state, runtime):
        update = original(state, runtime)
        if len(state.get("tool_events", [])) == 2:
            time.sleep(1.1)
        return update
    monkeypatch.setattr(readonly_workflow, "observe", slow_observe)
    result = _invoke(context)
    assert result["task_status"] == "failed" and "超时" in result["final_response"]
    assert len([event for event in result["tool_events"] if event.status == "ok"]) == 2
    assert not result["final_evidence"]


def test_cancellation_before_parent_report_cannot_succeed(tmp_path, monkeypatch):
    context, _ = _environment(tmp_path)
    original = delegated_workflow.report
    def cancel(state, runtime):
        context.cancellation.cancel("before-parent-report")
        return original(state, runtime)
    monkeypatch.setattr(delegated_workflow, "report", cancel)
    result = _invoke(context)
    assert result["task_status"] == "failed" and len(result["tool_events"]) == 4


def test_parent_scope_root_and_reviewer_subset(tmp_path):
    context, _ = _environment(tmp_path, body=json.dumps({"pattern": "docs/*.md", "keywords": ["取消", "执行结果", "能力"]}))
    context.delegated_research["tasks"][0]["scope"] = "."
    context.agent_capabilities = ("files_read", "code_search")
    result = _invoke(context)
    assert result["task_status"] == "completed", result["final_response"]
    assert all(f"docs/{name}.md:2" in result["final_response"] for name in ("cancel", "recovery", "permissions"))


def test_budget_shares_reject_all_before_any_model_starts(tmp_path):
    context, model = _environment(tmp_path, count=2, concurrency=2, limits={"max_model_tokens": 7000})
    context.delegated_research["tasks"][1]["question"] = "很长的问题" * 190
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert not model.requests and not result.get("tool_events")


def test_checkpoint_turns_append_each_child_once_and_normal_turn_does_not_redelegate(tmp_path, model_factory):
    context, _ = _environment(tmp_path, count=2, concurrency=2)
    saver = open_checkpointer(tmp_path.parent / "t67.sqlite")
    try:
        graph = builder.build_chat_graph(saver)
        config = {"configurable": {"thread_id": "t67"}}
        payload = {"user_input": "调查", "session_mode": "agent"}
        first = graph.invoke(payload, config=config, context=context)
        second = graph.invoke(payload, config=config, context=context)
        assert first["task_status"] == second["task_status"] == "completed"
        assert len(second["tool_events"]) == 8
        assert len(second["model_call_records"]) == len({record.model_call_id for record in second["model_call_records"]}) == 4
        assert len(second["turn_records"][second["run_id"]].model_call_ids) == 2
        normal = builder.build_runtime_context(app_config=context.app_config, model_adapter=model_factory(reply="普通回答"),
            workspace_root=tmp_path, current_cwd=tmp_path, cancellation=CancellationToken(),
            usage_recorder=context.usage_recorder)
        last = graph.invoke({"user_input": "解释概念", "session_mode": "agent"}, config=config, context=normal)
        assert last["workflow"] is None and last["final_response"] == "普通回答"
        assert len(last["tool_events"]) == 8
        assert all("child" not in identity for identity in last["turn_records"][last["run_id"]].model_call_ids)
    finally:
        saver.conn.close()


@pytest.mark.parametrize("tool_name", ["grep", "glob"])
def test_execution_rejects_search_spill_even_when_safety_is_bypassed(tmp_path, monkeypatch, tool_name):
    context, _ = _environment(tmp_path)
    original = readonly_workflow.prepare_call
    def inject(state, runtime):
        update = original(state, runtime)
        call = update["pending_tool_calls"][0]
        call.update(name=tool_name, args={"pattern": "." if tool_name == "grep" else "*.md", "save_full": True})
        update["messages"] = [AIMessage(content="", tool_calls=[call])]
        return update
    monkeypatch.setattr(readonly_workflow, "prepare_call", inject)
    monkeypatch.setattr(readonly_workflow, "readonly_safety", lambda state, runtime: {})
    result = _invoke(context)
    assert result["task_status"] == "failed"
    assert [event.status for event in result["tool_events"]] == ["denied"]
    assert not (tmp_path / "docs" / ".local-agent").exists()


@pytest.mark.parametrize("tool_name", ["grep", "glob"])
def test_direct_child_tool_cannot_spill_without_graph_gates(tmp_path, tool_name):
    context, _ = _environment(tmp_path)
    context.usage_recorder.bind_run("parent")
    item = delegated_workflow._prepare({"run_id": "parent", "session_mode": "agent"}, context)[0]
    child = delegated_workflow._child_context(context, item, CancellationToken())
    with pytest.raises(SecurityError, match="禁止保存"):
        child.tool_registry.get(tool_name).invoke({"pattern": "." if tool_name == "grep" else "*.md", "save_full": True})
    assert not (tmp_path / "docs" / ".local-agent").exists()
