"""T69：从公开提交入口验证工作流、幂等、审批、失败及检查点展示。"""

from __future__ import annotations

import time
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from test_code_workflow import ProposalModel
from test_delegated_workflow import ResearchModel

from local_agent.api.app import create_app
from local_agent.api.container import open_runtime
from local_agent.application.projection import project_workflow
from local_agent.config import WorkflowsConfig
from local_agent.workflow_eval import BROKEN_CODE, CODE_CASE, FIXED_CODE, READ_CASE, _config

READ = {"name": "readonly_research", "request": {"pattern": "docs/*.md", "keywords": []}}
CODE = {"name": "code_change", "request": {
    "source_path": "bounds.py", "test_paths": ["test_bounds.py"], "expected_test_count": 3}}
CHILD = {"name": "delegated_research", "request": {
    "tasks": [{"scope": "docs", "question": "核对原文"}], "concurrency": 1}}


@pytest.fixture
def workflow_api(tmp_path, model_factory, monkeypatch):
    monkeypatch.setenv("WORKFLOW_EVAL_UNUSED", "offline-placeholder")
    runtimes = []

    def open_one(*, model=None, limits=None, background=False, approval=False, reopen=False):
        if not reopen:
            for path, content in (*READ_CASE.seeds, *CODE_CASE.seeds):
                target = tmp_path / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content.encode())
        config = _config(tmp_path, "coder")
        config.workflows = WorkflowsConfig(enabled=True, **(limits or {}))
        config.memory.short_term.enabled = True
        config.memory.short_term.path = str(tmp_path / "data" / "checkpoints.sqlite")
        config.jobs.background = background
        config.jobs.max_concurrent_jobs = 1
        if approval:
            config.security.large_edit.max_changed_lines_without_approval = 1
        runtime = open_runtime(config, base_dir=tmp_path, model=model or model_factory(decision="chat", reply="普通回答"))
        runtimes.append(runtime)
        return TestClient(create_app(runtime), base_url="http://127.0.0.1"), runtime

    yield open_one
    for runtime in runtimes:
        runtime.service._watchdog_stop.set()
        runtime.service._jobs.close()
        runtime.close()


def _session(client):
    return client.post("/api/sessions", json={"title": "工作流"}).json()["session_id"]


def _send(client, sid, workflow, *, key="submit"):
    return client.post(f"/api/sessions/{sid}/messages", json={
        "text": "按指定范围验证", "client_request_id": key, "workflow": workflow})


def _snapshot(client, sid):
    response = client.get(f"/api/sessions/{sid}")
    assert response.status_code == 200, response.text
    return response.json()


def _wait(client, sid, terminal):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = _snapshot(client, sid)
        if snapshot["session_state"] in terminal:
            return snapshot
        time.sleep(0.02)
    pytest.fail(f"未到达 {terminal}: {snapshot}")


@pytest.mark.parametrize("workflow,kind", [(READ, "readonly_research"), (CODE, "code_change"), (CHILD, "delegated_research")])
def test_public_entry_runs_real_tools_and_replays_once(workflow_api, workflow, kind, tmp_path):
    model = ProposalModel(messages=iter([])) if workflow == CODE else ResearchModel(messages=iter([]))
    client, runtime = workflow_api(model=model)
    sid = _session(client)
    response = _send(client, sid, workflow)
    assert response.status_code == 200, response.text
    snapshot = _snapshot(client, sid)
    assert snapshot["session_state"] == "completed", snapshot
    assert snapshot["workflow"]["name"] == kind
    assert snapshot["workflow"]["phase"] == "completed"
    assert snapshot["workflow"]["evidence_count"] == (3 if workflow == CODE else 6)
    assert snapshot["final_result"]["state"] == "completed"
    assert snapshot["workflow"]["tool_calls"] > 0
    assert _send(client, sid, workflow).json()["replayed"] is True
    assert len(model.requests) == (0 if workflow == READ else 1)
    if workflow == CODE:
        assert (tmp_path / "bounds.py").read_text() == FIXED_CODE
    if workflow == CHILD:
        tasks = runtime.service.list_tasks().tasks
        assert len(tasks) == 1
        assert runtime.service._jobs.child_links(tasks[0].task_id)
    assert _snapshot(client, sid)["workflow"] == snapshot["workflow"]


@pytest.mark.parametrize("bad", [
    {"name": "unknown", "request": {}},
    {"name": "readonly_research", "request": {"pattern": "../*"}},
    {"name": "readonly_research", "request": {"pattern": "docs/*", "extra": True}},
    {"name": "code_change", "request": {**CODE["request"], "test_command": "python -c evil"}},
    {"name": "delegated_research", "request": {"tasks": []}},
])
def test_bad_contract_rejected_before_ledger_or_tools(workflow_api, bad):
    client, runtime = workflow_api()
    sid = _session(client)
    assert _send(client, sid, bad).status_code == 422
    assert _snapshot(client, sid)["messages"] == []
    assert runtime.service._ledger.get("submit") is None


def test_workflow_parameters_are_part_of_idempotency_and_not_sticky(workflow_api):
    client, runtime = workflow_api()
    sid = _session(client)
    assert _send(client, sid, READ).status_code == 200
    for changed in (None, CHILD, {"name": "readonly_research", "request": {"pattern": "docs/cancel.md"}}):
        response = _send(client, sid, changed)
        assert response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert _send(client, sid, None, key="normal").status_code == 200
    assert _snapshot(client, sid)["workflow"] is None
    assert runtime.model.decision_calls > 0


@pytest.mark.parametrize("limits,selection,reason", [
    ({"max_tool_calls": 1}, READ, "工具调用上限"),
    ({"max_steps": 1}, READ, "步骤上限"),
    ({"enabled": False}, READ, "未启用"),
    ({}, {"name": "readonly_research", "request": {"pattern": "missing/*"}}, "文件清单"),
    ({}, {"name": "readonly_research", "request": {"pattern": "docs/*", "keywords": ["不存在"]}}, "原文证据"),
    ({"max_model_tokens": 1}, CHILD, "预算"),
])
def test_failures_are_visible_on_parent(workflow_api, limits, selection, reason):
    enabled = limits.get("enabled", True)
    client, runtime = workflow_api(limits={key: value for key, value in limits.items() if key != "enabled"})
    runtime.service._app_config.workflows.enabled = enabled
    sid = _session(client)
    response = _send(client, sid, selection)
    assert response.status_code == 200, response.text
    snapshot = _snapshot(client, sid)
    assert snapshot["session_state"] == "failed"
    assert snapshot["workflow"]["phase"] == "failed"
    assert reason in snapshot["workflow"]["error"]
    assert snapshot["final_result"]["state"] == "failed"
    tasks = runtime.service.list_tasks().tasks
    assert len(tasks) == 1 and tasks[0].state == "failed"


@pytest.mark.parametrize("failure", ["raises", "timeout"])
def test_child_failure_and_timeout_visible(workflow_api, failure):
    model = ResearchModel(messages=iter([]), raises=failure == "raises", delay=1.1 if failure == "timeout" else 0)
    client, _ = workflow_api(model=model, limits={"child_timeout_seconds": 1})
    sid = _session(client)
    assert _send(client, sid, CHILD).status_code == 200
    snapshot = _snapshot(client, sid)
    assert snapshot["session_state"] == snapshot["workflow"]["phase"] == "failed"
    assert snapshot["workflow"]["children"][0]["status"] != "completed"
    assert snapshot["workflow"]["children"][0]["error"]
    assert len(model.requests) == 1


@pytest.mark.parametrize("decision", ["approve", "reject", "cancel"])
def test_parent_approval_uses_original_request_after_restart(workflow_api, decision, tmp_path):
    model = ProposalModel(messages=iter([]))
    client, runtime = workflow_api(model=model, approval=True)
    sid = _session(client)
    assert _send(client, sid, CODE).status_code == 200
    waiting = _snapshot(client, sid)
    assert waiting["session_state"] == "awaiting_approval" and waiting["workflow"]["phase"] == "waiting"
    assert len(model.requests) == 1
    if decision == "cancel":
        assert client.post(f"/api/sessions/{sid}/cancel", json={"reason": "保留源码"}).status_code == 200
    runtime.close()
    client2, runtime2 = workflow_api(model=model, approval=True, reopen=True)
    # Changed process defaults cannot shrink an already accepted turn's frozen budget.
    runtime2.service._app_config.workflows.max_tool_calls = 1
    snapshot = _snapshot(client2, sid)
    response = client2.post(f"/api/sessions/{sid}/approvals/{snapshot['approval']['approval_id']}", json={
        "decision": "reject" if decision == "reject" else "approve", "client_request_id": "decide"})
    assert response.status_code == 200, response.text
    final = _snapshot(client2, sid)
    assert final["workflow"]["phase"] == ("completed" if decision == "approve" else "failed")
    assert (tmp_path / "bounds.py").read_text() == (FIXED_CODE if decision == "approve" else BROKEN_CODE)
    assert len(model.requests) == 1


def test_background_waiting_approval_releases_worker_for_other_session(workflow_api):
    client, _ = workflow_api(model=ProposalModel(messages=iter([])), background=True, approval=True)
    first, second = _session(client), _session(client)
    assert _send(client, first, CODE).status_code == 200
    _wait(client, first, {"awaiting_approval"})
    assert _send(client, second, READ, key="second").status_code == 200
    assert _wait(client, second, {"completed", "failed"})["session_state"] == "completed"
    assert _snapshot(client, first)["session_state"] == "awaiting_approval"


def test_nested_checkpoint_progress_and_cancel_visible(workflow_api):
    started, release = Event(), Event()

    class Gate:
        def wait(self, timeout):
            started.set()
            assert release.wait(timeout)

    model = ResearchModel(messages=iter([]), gate=Gate())
    client, runtime = workflow_api(model=model)
    sid = _session(client)
    subscription = runtime.service.subscribe_events(sid)
    replies = []
    thread = Thread(target=lambda: replies.append(_send(client, sid, CHILD)))
    thread.start()
    try:
        assert started.wait(5)
        snapshot = _snapshot(client, sid)
        assert snapshot["session_state"] == "running"
        assert snapshot["workflow"]["name"] == "delegated_research"
        assert snapshot["workflow"]["model_tokens_reserved"] > 0
        assert client.post(f"/api/sessions/{sid}/cancel", json={"reason": "停止委派"}).status_code == 200
    finally:
        release.set()
        thread.join(10)
        events = subscription.poll()
        subscription.close()
    assert not thread.is_alive() and replies[0].status_code == 200
    progress = [event for event in events
                if event.event_type == "session_snapshot" and event.payload.workflow is not None]
    assert len(progress) >= 2
    assert len({event.event_id for event in progress}) == len(progress)
    assert _snapshot(client, sid)["workflow"]["phase"] == "failed"
    assert runtime.service.list_tasks().tasks[0].state == "cancelled"


def test_chat_mode_does_not_authorize_selected_workflow(workflow_api):
    client, runtime = workflow_api()
    sid = _session(client)
    response = client.patch(f"/api/sessions/{sid}/mode", json={"mode": "chat", "expected_mode": "agent"})
    assert response.status_code == 200
    assert _send(client, sid, READ).status_code == 200
    snapshot = _snapshot(client, sid)
    assert snapshot["workflow"]["phase"] == "failed"
    assert snapshot["workflow"]["tool_calls"] == 0
    assert runtime.model.decision_calls == 0


def test_projection_only_same_run_public_fields():
    state = {"run_id": "parent", "workflow": {
        "name": "code_change", "run_id": "parent", "phase": "model", "steps": 2,
        "model_prompt": "private prompt", "expected_content": "private source", "child_specs": ["private"],
        "error": "sk-secret-value", "call": {"args": {"command": "private"}}}}
    view = project_workflow(state, secrets=["sk-secret-value"])
    assert view is not None
    assert "private" not in view.model_dump_json() and "sk-secret-value" not in view.model_dump_json()
    assert project_workflow({**state, "run_id": "other"}) is None
